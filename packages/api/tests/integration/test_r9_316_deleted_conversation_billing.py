"""R9-316: a web turn whose write-back is refused after it streamed is charged once.

The conversation DELETE route now cancels a live turn in this process first, so that
turn ends ``cancelled`` and free. This file covers what is left: the row deleted from
elsewhere (another api process, an operator), which this process cannot cancel, so the
turn streams its whole answer and then its write-back is refused.

Drives the REAL chain the M2 billing tests use (``test_m2_i2_multiround_billing.py``):
a persona seeded in Postgres, ``RuntimeFactory.build_conversation_loop`` building a
real :class:`ConversationLoop` that writes through the REAL Postgres episodic store
and :class:`PostgresTurnLogWriter`, the REAL detached worker, and the REAL
:class:`MeteredCreditsPolicy` ledger. The only fake is the scripted backend, which
deletes the conversation row while it streams the answer (no network). The registry
is the web chat's shape (``delivers_streamed_text_on_error=True``).

Two ways the write-back fails once the row is gone, and both must end in exactly one
charge for the served answer:

* a plain delete: the episodic write is allowed (a plain delete keeps memory, so one
  stamped chunk is left), then the ``turn_logs`` insert is refused (RLS WITH CHECK
  under ``persona_app``, or the foreign key to ``conversations``);
* a delete that forgets the memory (PR #51, migration 068): the conversation is
  marked forgotten and the episodic write is refused first (no chunk is left).

Integration-marked (real Postgres, RLS-scoped); skips when APP_DATABASE_URL is unset.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.schema.conversation import Conversation
from persona_api.background.chat_turn_worker import ChatTurnRegistry, served_turn_billing_key
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

_AUDIT_ROOT = Path("/tmp/persona-r9-316-audit")  # noqa: S108  # the M1-T8 audit-root idiom
_ANSWER = "Here is the whole answer, streamed before the row went."


class _DeletingBackend:
    """Streams an answer and, mid-stream, deletes the conversation (as the user did)."""

    max_tokens = 4096
    provider_name = "openrouter"
    model_name = "zz/r9-316-model"

    def __init__(
        self, *, su_url: str, owner: str, persona_id: str, conv_id: str, forget: bool
    ) -> None:
        self._su_url = su_url
        self._owner = owner
        self._persona_id = persona_id
        self._conv_id = conv_id
        self._forget = forget
        self.calls = 0

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
        self.calls += 1
        yield StreamChunk(delta=_ANSWER, is_final=False)
        _delete_conversation(
            self._su_url, self._owner, self._persona_id, self._conv_id, forget=self._forget
        )
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=40, completion_tokens=10, total_tokens=50, cost_usd=0.03
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


class _RecordingSink:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    def checkpoint(self, **kwargs: object) -> None:  # noqa: ARG002
        return None

    def finalize(self, **kwargs: object) -> None:
        self.statuses.append(str(kwargs["status"]))


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
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p)"),
            {"c": conv_id, "o": owner, "p": persona_id},
        )
        # The month's free allowance is stamped as already refreshed, so the idempotent
        # deduct's lazy refresh (Spec M4 T6) leaves the seeded 100 alone.
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance, allowance_period) "
                "VALUES (:u, 100, to_char((now() AT TIME ZONE 'UTC'), 'YYYY-MM')) "
                "ON CONFLICT (user_id) DO UPDATE SET balance = 100, "
                "allowance_period = EXCLUDED.allowance_period"
            ),
            {"u": owner},
        )
    su.dispose()


def _delete_conversation(
    su_url: str, owner: str, persona_id: str, conv_id: str, *, forget: bool
) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        if forget:
            # What the forget_memory delete commits with the row delete (PR #51).
            conn.execute(
                text(
                    "INSERT INTO forgotten_conversations (conversation_id, owner_id, persona_id) "
                    "VALUES (:c, :o, :p)"
                ),
                {"c": conv_id, "o": owner, "p": persona_id},
            )
        conn.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": conv_id})
    su.dispose()


def _cleanup(su_url: str, owner: str, persona_id: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM memory_chunks WHERE persona_id = :p"), {"p": persona_id})
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
    su.dispose()


def _stamped_episodic_chunks(su_url: str, persona_id: str, conv_id: str) -> int:
    """Episodic chunks this turn left, stamped with the conversation (via the superuser)."""
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        count = int(
            conn.execute(
                text(
                    "SELECT count(*) FROM memory_chunks WHERE persona_id = :p "
                    "AND kind = 'episodic' AND metadata ->> 'conversation_id' = :c"
                ),
                {"p": persona_id, "c": conv_id},
            ).scalar_one()
        )
    su.dispose()
    return count


def _ledger(
    su_url: str, owner: str, conv_id: str
) -> tuple[list[tuple[int, str, str | None]], int, int]:
    """(ledger rows, balance, turn_logs rows for the conversation) via the superuser."""
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        rows = [
            (int(r[0]), str(r[1]), r[2])
            for r in conn.execute(
                text(
                    "SELECT delta, reason, billing_key FROM credit_transactions "
                    "WHERE user_id = :u ORDER BY created_at, id"
                ),
                {"u": owner},
            ).all()
        ]
        balance = int(
            conn.execute(
                text("SELECT balance FROM credits WHERE user_id = :u"), {"u": owner}
            ).scalar_one()
        )
        logs = int(
            conn.execute(
                text("SELECT count(*) FROM turn_logs WHERE conversation_id = :c"), {"c": conv_id}
            ).scalar_one()
        )
    su.dispose()
    return rows, balance, logs


async def _drive_and_bill_twice(
    *,
    app_url: str,
    su_url: str,
    embedder: HashEmbedder384,
    owner: str,
    persona_id: str,
    conv_id: str,
    forget: bool,
) -> tuple[_RecordingSink, list[tuple[str, object]], _DeletingBackend]:
    """Run the turn through the real worker, then bill the same turn a second time."""
    backend = _DeletingBackend(
        su_url=su_url, owner=owner, persona_id=persona_id, conv_id=conv_id, forget=forget
    )
    rls_engine = make_rls_engine(app_url)
    factory = RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=_ScriptedRegistry(backend),  # type: ignore[arg-type]
        turn_log_writer=PostgresTurnLogWriter(rls_engine),
        audit_root=_AUDIT_ROOT,
    )
    sink = _RecordingSink()
    registry = ChatTurnRegistry(
        sink=sink,  # type: ignore[arg-type]
        rls_engine=rls_engine,
        credits_policy=MeteredCreditsPolicy(),
        credits_per_turn=1,
        proportional_credits=True,
        delivers_streamed_text_on_error=True,
    )
    token = current_user_id.set(owner)
    items: list[tuple[str, object]] = []
    try:
        loop = await factory.build_conversation_loop(persona_id)
        handle = registry.start(
            conversation_id=conv_id,
            owner_id=owner,
            assistant_message_id=f"msg_{conv_id}",
            loop=loop,
            conversation=Conversation(conversation_id=conv_id, persona_id=persona_id, messages=[]),
            user_message="tell me the whole thing",
        )
        assert handle.task is not None
        await handle.task
        while (item := handle.events.get_nowait()) is not None:
            items.append(item)
        # A second attempt to bill the same turn: the real ledger refuses the key.
        registry._bill_served_failure(handle, loop)  # noqa: SLF001
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
    return sink, items, backend


@pytest.mark.asyncio
@pytest.mark.parametrize("forget", [False, True], ids=["plain-delete", "forget-memory-delete"])
async def test_a_turn_whose_conversation_is_deleted_mid_stream_is_charged_once(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
    forget: bool,
) -> None:
    app_url, su_url = _require_db_urls()
    suffix = "f" if forget else "p"
    owner, persona_id, conv = f"u_r9316_{suffix}", f"p_r9316_{suffix}", f"c_r9316_{suffix}"
    _seed(su_url, owner, persona_id, conv)
    try:
        sink, items, backend = await _drive_and_bill_twice(
            app_url=app_url,
            su_url=su_url,
            embedder=embedder,
            owner=owner,
            persona_id=persona_id,
            conv_id=conv,
            forget=forget,
        )
        ledger, balance, logs = _ledger(su_url, owner, conv)
        chunks = _stamped_episodic_chunks(su_url, persona_id, conv)
    finally:
        _cleanup(su_url, owner, persona_id)

    assert backend.calls == 1
    # The user saw the answer; the turn still ends as an error (nothing to keep it in).
    assert any(kind == "chunk" for kind, _payload in items)
    assert items[-1][0] == "error"
    assert sink.statuses == ["error"]
    assert logs == 0  # the turn could not be logged
    # The two cases fail at different steps: a plain delete keeps memory, so the episodic
    # write landed and the turn_logs insert was refused; a forget-memory delete refused
    # the episodic write itself, so nothing was written.
    assert chunks == (0 if forget else 1)
    # Charged exactly once, from the served usage: 0.03 USD actual = 3.0 cents = 3 credits.
    charges = [row for row in ledger if row[0] < 0]
    assert charges == [(-3, "chat_turn:actual_openrouter", served_turn_billing_key(f"msg_{conv}"))]
    assert balance == 97
