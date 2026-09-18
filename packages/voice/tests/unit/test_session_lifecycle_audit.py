"""R9-184: the voice session lifecycle seam must reach a durable record.

THE DEFECT CLASS. ``SessionStateMachine`` produces ``SESSION_CREATED`` /
``SESSION_ACTIVE`` / ``SESSION_ENDED`` on the V4 ``SessionEventListener`` seam, and
the agent runner built the machine with ``on_event=None``, so all three dispatched
into nothing in production. A producer with no consumer (completion-sweep shape 7):
nothing goes red, because an event nobody reads has no failing test.

WHAT THESE TESTS COVER.

1. The three lifecycle events reach the audit record, in order, with the session id,
   persona id, owner and a tz-aware UTC timestamp on each row.
2. A call that dies during connect (ended before it was ever active) still leaves
   CREATED and ENDED on the record, and no ACTIVE.
3. The four V4 speaking events ride the SAME seam via ``notify()`` and must NOT
   land on the lifecycle record.
4. A sink that raises does not break the state machine (fail-soft: a failed audit
   write degrades the record, never the call).
5. The runner actually composes the listener (the structural half; without it the
   behaviour above is unreachable and the whole thing ships dark again).
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from persona.audit import AuditAction, AuditEvent, MemoryAuditLogger
from persona.schema.chunks import WriteSource
from persona_voice.agent import runner
from persona_voice.session.lifecycle_audit import SessionLifecycleAuditor
from persona_voice.session.state_machine import SessionLifecycleEvent, SessionStateMachine

if TYPE_CHECKING:
    from persona.audit import AuditLogger

PERSONA_ID = "p-lifecycle"
USER_ID = "u-owner"


def _build_sm(audit: AuditLogger) -> SessionStateMachine:
    """A state machine whose lifecycle seam is wired to ``audit``."""
    return SessionStateMachine(
        session_id="sess-1",
        user_id=USER_ID,
        persona_id=PERSONA_ID,
        conversation_id="conv-1",
        rls_engine=MagicMock(),
        on_event=SessionLifecycleAuditor(audit_logger=audit),
    )


def _actions(audit: MemoryAuditLogger) -> list[AuditAction]:
    return [e.action for e in audit.read(PERSONA_ID, store="voice_session")]


# ---------- the record itself ----------------------------------------------


@pytest.mark.asyncio
async def test_full_session_leaves_all_three_events_in_order() -> None:
    audit = MemoryAuditLogger()
    sm = _build_sm(audit)

    await sm.mark_created()
    await sm.mark_active()
    await sm.end()

    assert _actions(audit) == [
        AuditAction.SESSION_CREATED,
        AuditAction.SESSION_ACTIVE,
        AuditAction.SESSION_ENDED,
    ]


@pytest.mark.asyncio
async def test_session_ended_before_active_leaves_created_and_ended() -> None:
    """The call died during connect. It still has a lifecycle trace."""
    audit = MemoryAuditLogger()
    sm = _build_sm(audit)

    await sm.mark_created()
    await sm.end()

    assert _actions(audit) == [AuditAction.SESSION_CREATED, AuditAction.SESSION_ENDED]


@pytest.mark.asyncio
async def test_a_session_that_never_announced_still_records_create_first() -> None:
    """``end()`` announces the create itself, so the record never starts at ENDED."""
    audit = MemoryAuditLogger()
    sm = _build_sm(audit)

    await sm.end()

    assert _actions(audit) == [AuditAction.SESSION_CREATED, AuditAction.SESSION_ENDED]


@pytest.mark.asyncio
async def test_each_row_carries_session_persona_owner_and_a_tz_aware_utc_timestamp() -> None:
    audit = MemoryAuditLogger()
    sm = _build_sm(audit)

    await sm.mark_active()
    await sm.end()

    rows = audit.read(PERSONA_ID, store="voice_session")
    assert len(rows) == 3
    for row in rows:
        assert row.persona_id == PERSONA_ID
        assert row.written_by == USER_ID
        assert row.source is WriteSource.SYSTEM
        assert row.metadata["session_id"] == "sess-1"
        assert row.metadata["conversation_id"] == "conv-1"
        assert row.timestamp.tzinfo is not None
        assert row.timestamp.utcoffset() == UTC.utcoffset(None)


@pytest.mark.asyncio
async def test_the_ended_row_records_the_ended_state() -> None:
    """The snapshot is taken AFTER the transition, so the record is not misleading."""
    audit = MemoryAuditLogger()
    sm = _build_sm(audit)

    await sm.mark_active()
    await sm.end()

    by_action = {e.action: e for e in audit.read(PERSONA_ID, store="voice_session")}
    assert by_action[AuditAction.SESSION_CREATED].metadata["state"] == "created"
    assert by_action[AuditAction.SESSION_ACTIVE].metadata["state"] == "active"
    assert by_action[AuditAction.SESSION_ENDED].metadata["state"] == "ended"


@pytest.mark.asyncio
async def test_turn_traffic_on_the_same_seam_does_not_reach_the_lifecycle_record() -> None:
    """V4's bridge pushes speaking events through ``notify()`` onto this same listener."""
    audit = MemoryAuditLogger()
    sm = _build_sm(audit)

    for ev in (
        SessionLifecycleEvent.USER_STARTED_SPEAKING,
        SessionLifecycleEvent.USER_STOPPED_SPEAKING,
        SessionLifecycleEvent.AGENT_STARTED_SPEAKING,
        SessionLifecycleEvent.AGENT_STOPPED_SPEAKING,
    ):
        await sm.notify(ev)

    assert audit.read(PERSONA_ID) == []


@pytest.mark.asyncio
async def test_the_clock_is_injectable_and_rows_are_ordered_by_it() -> None:
    stamps = iter(
        [
            datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC),
            datetime(2026, 9, 18, 10, 0, 1, tzinfo=UTC),
            datetime(2026, 9, 18, 10, 0, 2, tzinfo=UTC),
        ]
    )
    audit = MemoryAuditLogger()
    sm = SessionStateMachine(
        session_id="sess-1",
        user_id=USER_ID,
        persona_id=PERSONA_ID,
        conversation_id="conv-1",
        rls_engine=MagicMock(),
        on_event=SessionLifecycleAuditor(audit_logger=audit, clock=lambda: next(stamps)),
    )

    await sm.mark_active()
    await sm.end()

    times = [e.timestamp for e in audit.read(PERSONA_ID, store="voice_session")]
    assert times == sorted(times)
    assert times[0] == datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)


# ---------- fail-soft --------------------------------------------------------


class _ExplodingAudit:
    """An audit sink that fails every write, the way a full disk would."""

    def emit(self, _event: AuditEvent) -> None:
        msg = "audit sink is down"
        raise OSError(msg)

    def read(self, _persona_id: str, **_kwargs: object) -> list[AuditEvent]:
        return []


@pytest.mark.asyncio
async def test_a_sink_that_raises_does_not_break_the_state_machine() -> None:
    sm = SessionStateMachine(
        session_id="sess-1",
        user_id=USER_ID,
        persona_id=PERSONA_ID,
        conversation_id="conv-1",
        rls_engine=MagicMock(),
        on_event=SessionLifecycleAuditor(audit_logger=_ExplodingAudit()),
    )

    await sm.mark_active()
    assert sm.state == "active"
    await sm.end()
    assert sm.state == "ended"


@pytest.mark.asyncio
async def test_a_sink_that_raises_still_lets_the_room_teardown_dispose_the_engine() -> None:
    engine = MagicMock()
    sm = SessionStateMachine(
        session_id="sess-1",
        user_id=USER_ID,
        persona_id=PERSONA_ID,
        conversation_id="conv-1",
        rls_engine=engine,
        on_event=SessionLifecycleAuditor(audit_logger=_ExplodingAudit()),
    )

    await sm.end()

    engine.dispose.assert_called_once()


# ---------- the production call site ----------------------------------------


def _session_state_machine_call() -> ast.Call:
    """The ``SessionStateMachine(...)`` construction inside the agent runner."""
    source = inspect.getsource(runner)
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SessionStateMachine"
    ]
    assert len(calls) == 1, f"expected exactly one runner construction site, found {len(calls)}"
    return calls[0]


def test_the_runner_composes_a_lifecycle_listener() -> None:
    """Without this keyword every lifecycle event dispatches into ``None`` (R9-184)."""
    call = _session_state_machine_call()
    on_event = [kw for kw in call.keywords if kw.arg == "on_event"]
    assert on_event, "runner builds SessionStateMachine with no on_event listener"
    assert not any(kw.arg is None for kw in call.keywords), (
        "a **kwargs splat would make this guard blind"
    )


def test_the_runner_listener_is_the_lifecycle_auditor() -> None:
    call = _session_state_machine_call()
    (on_event,) = [kw.value for kw in call.keywords if kw.arg == "on_event"]
    assert isinstance(on_event, ast.Call)
    assert isinstance(on_event.func, ast.Name)
    assert on_event.func.id == "SessionLifecycleAuditor"
