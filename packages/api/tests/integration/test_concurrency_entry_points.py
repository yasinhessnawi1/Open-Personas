"""Long-op concurrency cap wired at the chat + agentic entry points (Spec R7, T9).

Proves the wiring around the durable-count admission at the two long-op entry points:

- Over-cap ⇒ ``ConcurrencyCappedError`` (the shipped 429 + ``Retry-After`` handler),
  raised BEFORE any persist — no orphan ``running`` message/run row is written (a
  post-persist refusal would wedge the one-active-turn / run surfaces).
- The worker's terminal ``finally`` release actually frees the slot (deletes the
  ``inflight_ops`` row) — the completion path the durable cap depends on.

(The admit/release/crash-safety semantics themselves are proven with real parallelism
in ``test_concurrency_caps.py``; this file proves the entry-point + worker wiring.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona_api.background.chat_turn_worker import ChatTurnHandle, ChatTurnRegistry
from persona_api.background.run_worker import RunHandle, RunRegistry
from persona_api.errors import ConcurrencyCappedError
from persona_api.services import chat_service, run_service
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER = "u_entry"
_PERSONA = "p_entry"


@pytest.fixture
def seeded_engine(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _USER, "e": f"{_USER}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'y') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": _PERSONA, "u": _USER},
        )
    return pg_engine


def _fill_inflight(engine: Engine, op_class: str, n: int) -> None:
    with engine.begin() as conn:
        for i in range(n):
            conn.execute(
                text("INSERT INTO inflight_ops (id, user_id, op_class) VALUES (:id, :u, :c)"),
                {"id": f"{op_class}_{i}", "u": _USER, "c": op_class},
            )


def _count(engine: Engine, table: str, where: str) -> int:
    with engine.begin() as conn:
        return int(conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}")).scalar_one())


@pytest.mark.asyncio
async def test_chat_entry_over_cap_raises_before_persist(seeded_engine: Engine) -> None:
    """A user at the chat long-op cap → ConcurrencyCappedError, and NO message row is
    written (the refusal fires before ``open_turn``)."""
    engine = seeded_engine
    cap = 3
    _fill_inflight(engine, "chat", cap)

    registry = MagicMock()
    registry.get.return_value = None  # no active turn for this conversation
    sink = MagicMock()
    loop_builder = AsyncMock()  # must never be awaited — the refusal is pre-build

    with pytest.raises(ConcurrencyCappedError) as ei:
        await chat_service.start_chat_turn(
            rls_engine=engine,
            sink=sink,
            registry=registry,
            loop_builder=loop_builder,
            owner_id=_USER,
            conversation_id="c_entry",
            user_message="hi",
            channel=None,
            max_concurrent_long_ops=cap,
        )
    assert ei.value.context["user_id"] == _USER
    assert "retry_after_s" in ei.value.context  # feeds the 429 Retry-After header
    sink.open_turn.assert_not_called()  # no persist happened
    loop_builder.assert_not_awaited()  # no loop build happened
    # The over-cap admit wrote no extra inflight row (count stays at the cap).
    assert _count(engine, "inflight_ops", f"user_id='{_USER}' AND op_class='chat'") == cap


@pytest.mark.asyncio
async def test_agentic_entry_over_cap_raises_before_persist(seeded_engine: Engine) -> None:
    """A user at the agentic long-op cap → ConcurrencyCappedError, and NO run row is
    persisted (the refusal fires before the ``runs`` INSERT)."""
    engine = seeded_engine
    cap = 3
    _fill_inflight(engine, "agentic", cap)

    registry = MagicMock()
    loop_builder = AsyncMock()

    with pytest.raises(ConcurrencyCappedError) as ei:
        await run_service.start_run(
            rls_engine=engine,
            registry=registry,
            loop_builder=loop_builder,
            owner_id=_USER,
            persona_id=_PERSONA,
            task="do it",
            max_concurrent_long_ops=cap,
        )
    assert ei.value.context["user_id"] == _USER
    loop_builder.assert_not_awaited()
    registry.start.assert_not_called()
    assert _count(engine, "runs", f"owner_id='{_USER}'") == 0, "no orphan run row persisted"


def test_chat_worker_finally_release_frees_slot(seeded_engine: Engine) -> None:
    """The chat worker's terminal release deletes the reserved inflight_ops row."""
    engine = seeded_engine
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inflight_ops (id, user_id, op_class) VALUES ('tok_chat', :u, 'chat')"
            ),
            {"u": _USER},
        )
    registry = ChatTurnRegistry(sink=MagicMock(), rls_engine=engine)
    handle = ChatTurnHandle("c_entry", _USER, "m1")
    handle.op_token = "tok_chat"

    registry._release_op_slot(handle)

    assert _count(engine, "inflight_ops", "id='tok_chat'") == 0, "release must delete the slot row"


def test_run_worker_finally_release_frees_slot(seeded_engine: Engine) -> None:
    """The run worker's terminal release deletes the reserved inflight_ops row."""
    engine = seeded_engine
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inflight_ops (id, user_id, op_class) VALUES ('tok_run', :u, 'agentic')"
            ),
            {"u": _USER},
        )
    registry = RunRegistry(engine)
    handle = RunHandle("run_entry", _USER)
    handle.op_token = "tok_run"

    registry._release_op_slot(handle)

    assert _count(engine, "inflight_ops", "id='tok_run'") == 0, "release must delete the slot row"
