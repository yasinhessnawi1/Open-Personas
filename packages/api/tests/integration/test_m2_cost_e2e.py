"""M2-T7 — end-to-end proof: one pricing truth, factory to ledger (Spec M2).

Drives the REAL production chain per the M1-T8 precedent (no hand-forced
state): a persona seeded in Postgres, ``RuntimeFactory.build_conversation_loop``
— the SAME composition-root method every chat request calls — builds a real
:class:`ConversationLoop` writing through the REAL
:class:`PostgresTurnLogWriter`, and the REAL detached worker
(:class:`ChatTurnRegistry`) drives the turn and bills it through the REAL
:class:`MeteredCreditsPolicy`. The only fakes are the scripted tier backends
(no network — the T20/T10 precedent).

Four scenarios:

(1) an OpenRouter-served turn whose final usage carries the response-side
    ACTUAL → the ``turn_logs`` row records ``actual_openrouter`` + the served
    model, and the ledger row charges ``ceil(actual cents)`` with the basis in
    its reason (D-M2-3 ∘ D-M2-5);
(2) a direct-provider turn → ``estimate_static`` at the vendor-verified rate,
    floor-charged (D-M2-1);
(3) an unknown model → ``unpriced`` + cost 0.0 + the flat floor with the bare
    reason — the absent-data invariant end to end (M2 §2);
(4) a genuine fallback (primary 429s inside its stream) → the row names AND
    prices the served OpenRouter secondary with its actual (D-M2-2 ∘ D-M2-3).

Plus BOTH fail-closed composition pins (M1-T8 precedent — deleting the wiring
fails this suite):

(A) every factory-built loop carries the factory's OWN metadata chain as
    ``cost_source`` (delete ``cost_source=self._metadata_resolver`` in
    runtime_factory → this fails);
(B) the PRODUCTION passthrough path opts into OpenRouter usage accounting
    (delete the ``_openai_extra_body`` merge → this fails).

Integration-marked (real Postgres, RLS-scoped); skips when APP_DATABASE_URL
is unset.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.errors import RateLimitError
from persona.backends.metadata import ChainedModelMetadataResolver
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.openrouter_passthrough import build_openrouter_passthrough
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

_AUDIT_ROOT = Path("/tmp/persona-m2e2e-audit")  # noqa: S108 — the M1-T8 audit-root idiom


class _ScriptedTierBackend:
    """A scripted tier backend; optionally OpenRouter-flavoured with an actual."""

    max_tokens = 4096

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        cost_usd: float | None = None,
        text_out: str = "Jeg er Astrid.",
    ) -> None:
        self.provider_name = provider
        self.model_name = model
        self._cost_usd = cost_usd
        self._text = text_out
        self.chat_stream_calls = 0

    @property
    def supports_native_tools(self) -> bool:
        return True

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
        yield StreamChunk(delta=self._text, is_final=False)
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cost_usd=self._cost_usd,
            ),
        )


class _FailingTierBackend:
    """A primary that genuinely 429s pre-first-chunk (the T2 double)."""

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
        raise RateLimitError(
            "scripted 429", context={"provider": self.provider_name, "status_code": "429"}
        )
        yield  # pragma: no cover — keeps this an async generator


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
                    "SELECT model_name, provider, cost_cents, cost_basis FROM turn_logs "
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
) -> RuntimeFactory:
    """Factory → real loop (real Postgres TurnLog writer) → real worker → real billing."""
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
            user_message="Hvem er du?",
        )
        assert handle.task is not None
        await handle.task
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
    return factory


# ----------------------------------------------------------------------------------- #
# (1) OpenRouter actual → row records the actual; the ledger charges ceil(actual).
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_openrouter_actual_prices_row_and_ledger_end_to_end(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
) -> None:
    app_url, su_url = _require_db_urls()
    owner, persona_id, conv = "u_m2e2e_a", "p_m2e2e_a", "c_m2e2e_a"
    _seed(su_url, owner, persona_id, conv)
    backend = _ScriptedTierBackend(
        provider="openrouter", model="zz/e2e-actual-model", cost_usd=0.055
    )
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

    assert backend.chat_stream_calls == 1
    # The persisted row: the ACTUAL, attributed to the served model (D-M2-3/4).
    assert row["cost_basis"] == "actual_openrouter"
    assert float(row["cost_cents"]) == pytest.approx(5.5)  # 0.055 USD
    assert row["model_name"] == "zz/e2e-actual-model"
    assert row["provider"] == "openrouter"
    # The bill: ceil(5.5) = 6 credits, basis in the reason (D-M2-5).
    assert ledger[-1] == (-6, "chat_turn:actual_openrouter")
    assert balance == 94


# ----------------------------------------------------------------------------------- #
# (2) Direct-provider estimate → static basis at the vendor-verified rate; floor bill.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_static_estimate_prices_row_and_ledger_end_to_end(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
) -> None:
    app_url, su_url = _require_db_urls()
    owner, persona_id, conv = "u_m2e2e_b", "p_m2e2e_b", "c_m2e2e_b"
    _seed(su_url, owner, persona_id, conv)
    backend = _ScriptedTierBackend(provider="anthropic", model="claude-sonnet-4-6")
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

    assert row["cost_basis"] == "estimate_static"
    # 10/5 tokens at $3/$15 per Mtok → 0.003 + 0.0075 cents.
    assert float(row["cost_cents"]) == pytest.approx(0.0105)
    assert row["model_name"] == "claude-sonnet-4-6"
    # Floor bill (ceil(0.0105) = 1), basis still audited in the reason.
    assert ledger[-1] == (-1, "chat_turn:estimate_static")
    assert balance == 99


# ----------------------------------------------------------------------------------- #
# (3) Unknown model → unpriced row + flat floor bill: the absent-data invariant.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unknown_model_is_unpriced_and_flat_billed_end_to_end(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
) -> None:
    app_url, su_url = _require_db_urls()
    owner, persona_id, conv = "u_m2e2e_c", "p_m2e2e_c", "c_m2e2e_c"
    _seed(su_url, owner, persona_id, conv)
    backend = _ScriptedTierBackend(provider="acme", model="mystery-e2e-model")
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

    # Honest degrade: never a guess (M2 §2)...
    assert row["cost_basis"] == "unpriced"
    assert float(row["cost_cents"]) == 0.0
    # ...and billing falls to the flat floor with the bare legacy reason.
    assert ledger[-1] == (-1, "chat_turn")
    assert balance == 99


# ----------------------------------------------------------------------------------- #
# (4) Genuine fallback → the served OpenRouter secondary is named, actual-priced, billed.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fallback_served_model_actual_end_to_end(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
) -> None:
    app_url, su_url = _require_db_urls()
    owner, persona_id, conv = "u_m2e2e_d", "p_m2e2e_d", "c_m2e2e_d"
    _seed(su_url, owner, persona_id, conv)
    primary = _FailingTierBackend(provider="anthropic", model="claude-sonnet-4-6")
    secondary = _ScriptedTierBackend(
        provider="openrouter", model="zz/e2e-fallback-model", cost_usd=0.021
    )
    wrapper = MultiModelChatBackend(
        [primary, secondary],  # type: ignore[list-item]
        tier_name="frontier",
        max_retries_per_backend=0,
    )
    try:
        await _drive_full_chain(
            app_url=app_url,
            embedder=embedder,
            backend=wrapper,
            owner=owner,
            persona_id=persona_id,
            conv_id=conv,
        )
        row, ledger, balance = _row_and_ledger(su_url, conv, owner)
    finally:
        _cleanup(su_url, owner)

    assert primary.chat_stream_calls == 1  # the primary genuinely ran and failed
    assert secondary.chat_stream_calls == 1
    # D-M2-2 ∘ D-M2-3: the row names AND actual-prices the SERVED secondary.
    assert row["model_name"] == "zz/e2e-fallback-model"
    assert row["provider"] == "openrouter"
    assert row["cost_basis"] == "actual_openrouter"
    assert float(row["cost_cents"]) == pytest.approx(2.1)
    assert ledger[-1] == (-3, "chat_turn:actual_openrouter")  # ceil(2.1)
    assert balance == 97


# ----------------------------------------------------------------------------------- #
# Fail-closed pins (M1-T8 precedent) — deleting the wiring fails this suite.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pin_factory_loop_carries_the_shared_cost_source(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
) -> None:
    """Pin A: delete ``cost_source=self._metadata_resolver`` → this fails."""
    app_url, su_url = _require_db_urls()
    owner, persona_id, conv = "u_m2e2e_pin", "p_m2e2e_pin", "c_m2e2e_pin"
    _seed(su_url, owner, persona_id, conv)
    rls_engine = make_rls_engine(app_url)
    factory = RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=_ScriptedRegistry(_ScriptedTierBackend(provider="anthropic", model="m")),  # type: ignore[arg-type]
        turn_log_writer=PostgresTurnLogWriter(rls_engine),
        audit_root=_AUDIT_ROOT,
    )
    token = current_user_id.set(owner)
    try:
        loop = await factory.build_conversation_loop(persona_id)
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
        _cleanup(su_url, owner)

    assert isinstance(factory._metadata_resolver, ChainedModelMetadataResolver)  # noqa: SLF001
    # The loop prices through the factory's OWN chain — same instance, never a
    # stub, never omitted (the D-M2-1 composition-root pin).
    assert loop._cost_source is factory._metadata_resolver  # noqa: SLF001


def test_pin_production_passthrough_opts_into_usage_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin B: delete the ``_openai_extra_body`` usage-include merge → this fails.

    Builds the REAL production passthrough (the same ``load_backend`` path a
    persona's ``preferred_model`` rides) and asserts its request payload will
    carry the D-M2-3 opt-in. No network — payload construction only.
    """
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-test-not-real")
    build_openrouter_passthrough.cache_clear()
    try:
        backend = build_openrouter_passthrough("zz/pin-model")
        assert backend is not None
        assert backend._openai_extra_body() == {"usage": {"include": True}}  # type: ignore[attr-defined]  # noqa: SLF001
    finally:
        build_openrouter_passthrough.cache_clear()
