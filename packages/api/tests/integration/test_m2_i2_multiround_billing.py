"""M2 review, finding I2 — multi-round usage/cost accumulation, billed for real.

Extends the M2-T7 / T5 e2e harness shape (``test_m2_cost_e2e.py``): drives the
REAL production chain — a persona seeded in Postgres,
``RuntimeFactory.build_conversation_loop`` (the SAME composition-root method
every chat request calls) builds a real :class:`ConversationLoop` writing
through the REAL :class:`PostgresTurnLogWriter`, and the REAL detached worker
(:class:`ChatTurnRegistry`) drives the turn and bills it through the REAL
:class:`MeteredCreditsPolicy`. The only fake is the scripted tier backend (no
network — the T20/T10 precedent).

The finding: across a turn's multiple rounds (tool rounds, the refusal-retry
round), ``ConversationLoop.turn()`` used to keep only the LAST round's
:class:`TokenUsage` (last-writer-wins, never summed) — a multi-round agentic
turn recorded and billed only its final round's tokens/actual. On OpenRouter
every round is a separately-billed request, so this understated what the
platform actually charged. This file proves the fix end to end: a 2-round
turn (the REAL refusal-retry re-generation, D-25-T21 — see the
``_MultiRoundActualBackend`` docstring for why this drives the second round
instead of a tool call), BOTH rounds carrying a genuine OpenRouter actual,
records and BILLS the SUM of both rounds' actuals through the REAL
``turn_logs`` row and the REAL ``credit_transactions`` ledger — not just the
last round's number.

Integration-marked (real Postgres, RLS-scoped); skips when APP_DATABASE_URL
is unset.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.schema.conversation import Conversation
from persona_api.background.chat_turn_worker import ChatTurnRegistry
from persona_api.editions.credits_policy import MeteredCreditsPolicy
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.services.turn_log_writer import PostgresTurnLogWriter
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends import ToolSpec
    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
self_facts:
  - fact: knows things
    confidence: 1.0
"""

_AUDIT_ROOT = Path("/tmp/persona-m2i2-audit")  # noqa: S108 — the M1-T8 audit-root idiom


#: Mirrors the proven ``test_refusal_auto_retry.py`` pattern, targeting
#: ``web_search`` — one of the 11 builtins ``build_default_toolbox`` composes
#: UNCONDITIONALLY (``persona/tools/_factory.py``'s ``builtins`` list; unlike
#: ``generate_image``/``code_execution`` it needs no ``image_backend`` /
#: ``sandbox_pool`` wiring this bare-factory harness doesn't provide) — so
#: this drives the loop's genuine refusal-retry re-generation (D-25-T21)
#: rather than a tool-calling round. Deliberately NOT a tool-calling round:
#: OpenRouter-provider tool dispatch goes through ``format_tool_result``,
#: which has no ``"openrouter"`` case — a separate, pre-existing gap outside
#: this fix's scope (no test anywhere in this repo had ever dispatched a REAL
#: tool call on an OpenRouter-flavoured backend before this finding's
#: test-writing surfaced it). Refusal-retry rounds are plain text on both
#: sides, so an ``openrouter``-provider turn is safe here.
_REFUSAL = "I'm sorry, but I can't search the web — I'm only a text-based assistant."
_CORRECTED = "Sure — searching the web now."


class _MultiRoundActualBackend:
    """A 2-round OpenRouter-flavoured backend — the real multi-round shape.

    Round 1 (call #1): a genuine tool refusal in plain text — the loop's REAL
    refusal-retry logic (armed via ``PERSONA_REFUSAL_RETRY_ENABLED``) detects
    it and re-generates exactly once (D-05-11 round counting). Carries its
    own OpenRouter actual.

    Round 2 (call #2): the corrective re-generation — plain text, a
    DIFFERENT actual. Ends the turn.
    """

    max_tokens = 4096

    def __init__(self, *, provider: str, model: str) -> None:
        self.provider_name = provider
        self.model_name = model
        self.chat_stream_calls = 0

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> object:  # noqa: ARG002
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002
        *,
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        self.chat_stream_calls += 1
        if self.chat_stream_calls == 1:
            yield StreamChunk(delta=_REFUSAL, is_final=False)
            yield StreamChunk(
                delta="",
                is_final=True,
                usage=TokenUsage(
                    prompt_tokens=40, completion_tokens=10, total_tokens=50, cost_usd=0.01
                ),
            )
            return
        yield StreamChunk(delta=_CORRECTED, is_final=False)
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=70, completion_tokens=20, total_tokens=90, cost_usd=0.02
            ),
        )


class _ScriptedRegistry:
    """A TierRegistry stand-in serving ONE backend for any tier (T10 precedent)."""

    def __init__(self, backend: object) -> None:
        self.backend = backend

    def get(self, _tier_name: str) -> object:
        return self.backend

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def supports_vision_for(self, _tier_name: str) -> bool:
        return False

    def metadata_for(self, _tier_name: str) -> None:
        return None

    def model_name_for(self, _tier_name: str) -> str:
        return "scripted"

    async def aclose(self) -> None:
        pass


class _NullSink:
    def checkpoint(self, **kwargs: object) -> None:  # noqa: ARG002
        return None

    def finalize(self, **kwargs: object) -> None:  # noqa: ARG002
        return None


def _require_db_urls() -> tuple[str, str]:
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    return app_url, os.environ["DATABASE_URL"]


def _seed(su_url: str, owner: str, persona_id: str, conv_id: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y)"),
            {"i": persona_id, "o": owner, "y": _YAML},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p) "
                "ON CONFLICT DO NOTHING"
            ),
            {"c": conv_id, "o": owner, "p": persona_id},
        )
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance) VALUES (:u, 100) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = 100"
            ),
            {"u": owner},
        )
    su.dispose()


def _cleanup(su_url: str, owner: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
    su.dispose()


def _row_and_ledger(
    su_url: str, conv_id: str, owner: str
) -> tuple[dict[str, object], list[tuple[int, str]], int]:
    """(latest turn_logs row, full ledger, balance) via the superuser engine."""
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT model_name, provider, prompt_tokens, completion_tokens, "
                    "cost_cents, cost_basis FROM turn_logs "
                    "WHERE conversation_id = :c ORDER BY created_at DESC, turn_index DESC LIMIT 1"
                ),
                {"c": conv_id},
            )
            .mappings()
            .one()
        )
        ledger = [
            (int(r[0]), str(r[1]))
            for r in conn.execute(
                text(
                    "SELECT delta, reason FROM credit_transactions WHERE user_id = :u "
                    "ORDER BY created_at, id"
                ),
                {"u": owner},
            ).all()
        ]
        balance = int(
            conn.execute(
                text("SELECT balance FROM credits WHERE user_id = :u"), {"u": owner}
            ).scalar_one()
        )
    su.dispose()
    return dict(row), ledger, balance


async def _drive_full_chain(
    *,
    app_url: str,
    embedder: HashEmbedder384,
    backend: object,
    owner: str,
    persona_id: str,
    conv_id: str,
) -> None:
    """Factory -> real loop (real Postgres TurnLog writer) -> real worker -> real billing."""
    rls_engine = make_rls_engine(app_url)
    factory = RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=_ScriptedRegistry(backend),  # type: ignore[arg-type]
        turn_log_writer=PostgresTurnLogWriter(rls_engine),
        audit_root=_AUDIT_ROOT,
    )
    registry = ChatTurnRegistry(
        sink=_NullSink(),  # type: ignore[arg-type]
        rls_engine=rls_engine,
        credits_policy=MeteredCreditsPolicy(),
        credits_per_turn=1,
        proportional_credits=True,
    )
    token = current_user_id.set(owner)
    try:
        loop = await factory.build_conversation_loop(persona_id)
        handle = registry.start(
            conversation_id=conv_id,
            owner_id=owner,
            assistant_message_id=f"{conv_id}_msg",
            loop=loop,
            conversation=Conversation(conversation_id=conv_id, persona_id=persona_id, messages=[]),
            user_message="please search the web for today's weather",
        )
        assert handle.task is not None
        await handle.task
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()


@pytest.mark.asyncio
async def test_multi_round_summed_actual_bills_correctly_end_to_end(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The billed amount rides the SUMMED cost through the real worker/ledger.

    Both rounds carry a genuine OpenRouter actual (0.01 USD + 0.02 USD) — the
    turn's true cost is their SUM (0.03 USD = 3.0 cents), billed as
    ``ceil(3.0) = 3`` credits. The pre-fix bug recorded and billed ONLY the
    last round (0.02 USD = 2.0 cents -> ceil 2 credits) — a real, provable
    1-credit undercharge on this exact scenario.
    """
    monkeypatch.setenv("PERSONA_REFUSAL_RETRY_ENABLED", "true")
    app_url, su_url = _require_db_urls()
    owner, persona_id, conv = "u_m2i2_a", "p_m2i2_a", "c_m2i2_a"
    _seed(su_url, owner, persona_id, conv)
    backend = _MultiRoundActualBackend(provider="openrouter", model="zz/i2-multiround-model")
    try:
        await _drive_full_chain(
            app_url=app_url,
            embedder=embedder,
            backend=backend,
            owner=owner,
            persona_id=persona_id,
            conv_id=conv,
        )
        row, ledger, balance = _row_and_ledger(su_url, conv, owner)
    finally:
        _cleanup(su_url, owner)

    # Both rounds genuinely ran (the real refusal-retry re-generation, not a
    # hand-forced state).
    assert backend.chat_stream_calls == 2
    # Tokens SUM across both rounds (40+70, 10+20) — never just the last round's.
    assert int(row["prompt_tokens"]) == 110
    assert int(row["completion_tokens"]) == 30
    # The persisted row: the SUMMED actual, attributed to the served model
    # (attribution stays last-round-served, per the fix's scope — D-M2-2
    # unchanged; only the token/cost fields now cover every round).
    assert row["cost_basis"] == "actual_openrouter"
    assert float(row["cost_cents"]) == pytest.approx(3.0)  # (0.01 + 0.02) USD
    assert row["model_name"] == "zz/i2-multiround-model"
    assert row["provider"] == "openrouter"
    # The bill: ceil(3.0) = 3 credits, basis in the reason (D-M2-5) — the
    # SUMMED amount, not the last-round-only ceil(2.0) = 2 the pre-fix loop
    # would have charged.
    assert ledger[-1] == (-3, "chat_turn:actual_openrouter")
    assert balance == 97
