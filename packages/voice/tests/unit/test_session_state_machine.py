"""Unit tests for :class:`SessionStateMachine` (spec V1 T06).

Cover state transitions (created → active → ended; idempotency; illegal
backward transitions), V4 event dispatch through the registered listener,
engine disposal at end(), and the :class:`VoiceRoom` integration that wires
LiveKit ``disconnected`` to ``end()``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona_voice.session.state_machine import (
    InvalidSessionStateError,
    Session,
    SessionLifecycleEvent,
    SessionStateMachine,
    make_session_rls_engine,
)
from pydantic import ValidationError

# ---------- Session model ---------------------------------------------------


def test_session_is_frozen_and_extra_forbid() -> None:
    s = Session(
        session_id="s1",
        user_id="u1",
        persona_id="p1",
        conversation_id="c1",
        state="created",
        created_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        Session(  # type: ignore[call-arg]
            session_id="s1",
            user_id="u1",
            persona_id="p1",
            conversation_id="c1",
            state="created",
            created_at=datetime.now(UTC),
            unknown_field="x",
        )
    # frozen: assignment after construction raises
    with pytest.raises(ValidationError):
        s.state = "active"  # type: ignore[misc]


def test_session_lifecycle_event_values_are_stable_strings() -> None:
    """Wire-format stability matters because T10 VoiceLog records the event
    name; if these strings change, replay-aggregate stops matching."""
    assert SessionLifecycleEvent.USER_STARTED_SPEAKING.value == "user_started_speaking"
    assert SessionLifecycleEvent.AGENT_STARTED_SPEAKING.value == "agent_started_speaking"
    assert SessionLifecycleEvent.SESSION_CREATED.value == "session_created"
    assert SessionLifecycleEvent.SESSION_ENDED.value == "session_ended"


# ---------- helpers ---------------------------------------------------------


def _build_sm(
    *,
    on_event: Any = None,  # noqa: ANN401
    clock: Any = None,  # noqa: ANN401
) -> SessionStateMachine:
    """Build a fresh state machine with a disposable mock engine."""
    engine = MagicMock()
    engine.dispose = MagicMock(return_value=None)
    return SessionStateMachine(
        session_id="sess_test",
        user_id="user_a",
        persona_id="p_astrid",
        conversation_id="c_chat",
        rls_engine=engine,
        on_event=on_event,
        clock=clock,
    )


# ---------- construction ----------------------------------------------------


def test_construction_starts_in_created_state() -> None:
    sm = _build_sm()
    assert sm.state == "created"
    snap = sm.session
    assert snap.session_id == "sess_test"
    assert snap.user_id == "user_a"
    assert snap.persona_id == "p_astrid"
    assert snap.conversation_id == "c_chat"
    assert snap.state == "created"
    assert snap.ended_at is None
    # created_at is timezone-aware (UTC per the default clock).
    assert snap.created_at.tzinfo is not None


def test_rls_engine_property_returns_injected_engine() -> None:
    fake_engine = MagicMock()
    sm = SessionStateMachine(
        session_id="s",
        user_id="u",
        persona_id="p",
        conversation_id="c",
        rls_engine=fake_engine,
    )
    assert sm.rls_engine is fake_engine


# ---------- transitions -----------------------------------------------------


@pytest.mark.asyncio
async def test_mark_created_announces_the_session_before_anything_else() -> None:
    """The create is a lifecycle event in its own right, dispatched at create.

    Without it the seam only ever heard about sessions that reached ``active``, so a
    call that died during connect left no trace at all.
    """
    events: list[tuple[SessionLifecycleEvent, str]] = []

    async def _on(ev: SessionLifecycleEvent, sess: Session) -> None:
        events.append((ev, sess.state))

    sm = _build_sm(on_event=_on)
    await sm.mark_created()
    assert events == [(SessionLifecycleEvent.SESSION_CREATED, "created")]


@pytest.mark.asyncio
async def test_mark_created_is_idempotent() -> None:
    on_event = AsyncMock()
    sm = _build_sm(on_event=on_event)
    await sm.mark_created()
    await sm.mark_created()
    await sm.mark_active()
    assert on_event.await_count == 2  # one create, one active


@pytest.mark.asyncio
async def test_a_session_that_ends_before_active_still_has_a_created_record() -> None:
    """The failure this exists for: an abrupt drop between create and active.

    The session never becomes ``active``, so ``SESSION_ACTIVE`` never fires; the create
    still has to be on record, and in the right order, or the end reads as the end of
    something that was never there.
    """
    events: list[tuple[SessionLifecycleEvent, str]] = []

    async def _on(ev: SessionLifecycleEvent, sess: Session) -> None:
        events.append((ev, sess.state))

    sm = _build_sm(on_event=_on)
    await sm.end()
    assert events == [
        (SessionLifecycleEvent.SESSION_CREATED, "created"),
        (SessionLifecycleEvent.SESSION_ENDED, "ended"),
    ]


@pytest.mark.asyncio
async def test_mark_active_transitions_created_to_active_and_emits_event() -> None:
    events: list[tuple[SessionLifecycleEvent, str]] = []

    async def _on(ev: SessionLifecycleEvent, sess: Session) -> None:
        events.append((ev, sess.state))

    sm = _build_sm(on_event=_on)
    await sm.mark_active()
    assert sm.state == "active"
    assert events == [
        (SessionLifecycleEvent.SESSION_CREATED, "created"),
        (SessionLifecycleEvent.SESSION_ACTIVE, "active"),
    ]


@pytest.mark.asyncio
async def test_mark_active_is_idempotent() -> None:
    on_event = AsyncMock()
    sm = _build_sm(on_event=on_event)
    await sm.mark_active()
    await sm.mark_active()
    assert sm.state == "active"
    # The transition fires once, after the one-time create announcement.
    assert on_event.await_count == 2


@pytest.mark.asyncio
async def test_end_disposes_engine_and_emits_event() -> None:
    events: list[SessionLifecycleEvent] = []

    async def _on(ev: SessionLifecycleEvent, _sess: Session) -> None:
        events.append(ev)

    sm = _build_sm(on_event=_on)
    await sm.mark_active()
    engine = sm.rls_engine
    await sm.end()
    assert sm.state == "ended"
    assert sm.session.ended_at is not None
    engine.dispose.assert_called_once()  # type: ignore[attr-defined]
    # Order matters: the create, then SESSION_ACTIVE, then SESSION_ENDED.
    assert events == [
        SessionLifecycleEvent.SESSION_CREATED,
        SessionLifecycleEvent.SESSION_ACTIVE,
        SessionLifecycleEvent.SESSION_ENDED,
    ]


@pytest.mark.asyncio
async def test_end_is_idempotent_and_does_not_redispose() -> None:
    sm = _build_sm()
    engine = sm.rls_engine
    await sm.end()
    await sm.end()
    assert sm.state == "ended"
    # Engine.dispose is called exactly once across both end() invocations.
    engine.dispose.assert_called_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_end_can_run_from_created_state_directly() -> None:
    """An abrupt disconnect during connect (before mark_active) is a
    legitimate scenario — ``end()`` must work from ``created`` too."""
    sm = _build_sm()
    assert sm.state == "created"
    await sm.end()
    assert sm.state == "ended"


@pytest.mark.asyncio
async def test_mark_active_after_end_raises() -> None:
    """A buggy caller cannot revive a closed session — guards against
    silent state regression in the audio loop."""
    sm = _build_sm()
    await sm.end()
    with pytest.raises(InvalidSessionStateError) as ei:
        await sm.mark_active()
    assert "ended" in ei.value.context.get("state", "")


# ---------- event dispatch --------------------------------------------------


@pytest.mark.asyncio
async def test_notify_fans_user_started_to_listener() -> None:
    on_event = AsyncMock()
    sm = _build_sm(on_event=on_event)
    await sm.notify(SessionLifecycleEvent.USER_STARTED_SPEAKING)
    on_event.assert_awaited_once()
    args, _ = on_event.call_args
    assert args[0] is SessionLifecycleEvent.USER_STARTED_SPEAKING
    assert isinstance(args[1], Session)


@pytest.mark.asyncio
async def test_notify_without_listener_is_silent_noop() -> None:
    """V1 ships the seam; V4 plugs in. Before V4, missing listener must
    not crash the audio loop."""
    sm = _build_sm()
    await sm.notify(SessionLifecycleEvent.AGENT_STOPPED_SPEAKING)  # no raise


# ---------- engine helper ---------------------------------------------------


def test_make_session_rls_engine_constructs_and_disposes_cleanly() -> None:
    """Smoke: the engine constructs without error and disposes cleanly.

    The RLS checkout-listener body fires on real Postgres connections only
    (the integration test in T11 exercises end-to-end against a real
    ``persona_app`` non-superuser role). Here we just confirm the factory
    wires SQLAlchemy correctly so other tests can rely on the same shape.
    """
    engine = make_session_rls_engine("sqlite://", user_id="user_a")
    # The engine exposes the expected SQLAlchemy interfaces.
    assert hasattr(engine, "connect")
    assert hasattr(engine, "dispose")
    engine.dispose()


# ---------- VoiceRoom integration ------------------------------------------


@pytest.mark.asyncio
async def test_attach_to_room_wires_disconnect_to_end() -> None:
    """``Room.on('disconnected')`` → ``SessionStateMachine.end()``."""
    voice_room = MagicMock()
    captured: dict[str, Any] = {}

    def _set_disconnect_handler(handler: Any) -> None:  # noqa: ANN401
        captured["handler"] = handler

    voice_room.set_disconnect_handler = _set_disconnect_handler

    sm = _build_sm()
    sm.attach_to_room(voice_room)
    assert "handler" in captured
    await captured["handler"]()
    assert sm.state == "ended"


# ---------- clock injection -------------------------------------------------


@pytest.mark.asyncio
async def test_clock_injection_produces_deterministic_timestamps() -> None:
    fixed_now = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)
    ticks = iter([fixed_now, fixed_now + timedelta(minutes=5)])
    sm = _build_sm(clock=lambda: next(ticks))
    assert sm.session.created_at == fixed_now
    await sm.end()
    assert sm.session.ended_at == fixed_now + timedelta(minutes=5)
