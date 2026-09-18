"""Per-call session lifecycle state machine for persona-voice (spec V1 T06).

One :class:`SessionStateMachine` instance per live WebRTC call. It owns:

1. **The Session record** — frozen Pydantic carrying ``session_id``,
   ``user_id``, ``persona_id``, ``conversation_id``, ``state`` (``created`` /
   ``active`` / ``ended``), ``created_at``, ``ended_at``.

2. **The per-session RLS engine** (D-V1-X-rls-engine-shape) — a SQLAlchemy
   ``Engine`` whose every connection runs
   ``SELECT set_config('app.current_user_id', :uid, false)`` on checkout, so
   every store query the audio loop issues is RLS-scoped to the call's
   user. ``pool_size=1`` because a session is single-tasked. The engine
   disposes on :meth:`SessionStateMachine.end`, freeing the underlying
   connection and releasing the per-user
   ``pg_try_advisory_xact_lock`` (T09) via transaction rollback if the
   session crashed mid-flight.

3. **The V4 lifecycle hook seams** — a :class:`SessionEventListener`
   Protocol that T07's dual-priority queue dispatches into. V1 ships the
   seam + the four canonical events; V4 (turn-taking + barge-in) plugs in
   the orchestration logic without re-touching V1.

The state machine integrates with :class:`persona_voice.transport.VoiceRoom`
via :meth:`SessionStateMachine.attach_to_room`: LiveKit
``participant_connected`` flips ``created`` → ``active``; LiveKit
``disconnected`` (clean or abrupt) flips ``active`` → ``ended`` and disposes
the engine. State transitions are guarded — a backward transition raises
:class:`InvalidSessionStateError` instead of silently regressing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, Protocol

from persona.errors import PersonaError
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, event

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from sqlalchemy import Engine

    from persona_voice.transport import VoiceRoom


__all__ = [
    "InvalidSessionStateError",
    "Session",
    "SessionEventListener",
    "SessionLifecycleEvent",
    "SessionState",
    "SessionStateMachine",
    "make_session_rls_engine",
]


SessionState = Literal["created", "active", "ended"]


class InvalidSessionStateError(PersonaError):
    """Raised on an illegal session-state transition.

    Examples: ``end()`` called twice; ``mark_active()`` called after
    ``end()``. Backward transitions are silenced into errors so a buggy
    caller never quietly puts a closed session back in flight.
    """


class Session(BaseModel):
    """Immutable snapshot of a voice session (boundary type — D-05-9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    user_id: str
    persona_id: str
    conversation_id: str
    state: SessionState
    created_at: datetime
    ended_at: datetime | None = None


class SessionLifecycleEvent(StrEnum):
    """V4 lifecycle hook events dispatched by T07's dual-priority queue.

    Names mirror R-V1-5's LiveKit Agents v1.5 SessionEvent vocabulary so V4
    consumers map cleanly to the convergent industry seam. Encoded as
    ``str`` so they survive JSON round-trips into the T10 VoiceLog audit
    record (``str`` is the cross-process-safe boundary form per D-05-9).
    """

    USER_STARTED_SPEAKING = "user_started_speaking"
    USER_STOPPED_SPEAKING = "user_stopped_speaking"
    AGENT_STARTED_SPEAKING = "agent_started_speaking"
    AGENT_STOPPED_SPEAKING = "agent_stopped_speaking"
    SESSION_CREATED = "session_created"
    SESSION_ACTIVE = "session_active"
    SESSION_ENDED = "session_ended"


class SessionEventListener(Protocol):
    """V4 seam — T07's dual-priority queue routes lifecycle events here.

    Async so V4 can push events into its own queue or trigger TTS-cancel
    without blocking T07's audio drain. V1 ships the Protocol + a no-op
    default; V4's actual orchestration is downstream scope.
    """

    async def __call__(self, event: SessionLifecycleEvent, session: Session) -> None: ...


# psycopg3 raw-cursor SQL. ``set_config`` is the parameterised form
# spec-08 D-07-5 documents — ``SET LOCAL app.current_user_id = $1`` is a
# Postgres syntax error with bound parameters. ``false`` makes the setting
# session-scoped; since the engine pool is sized 1 and owned by this session
# for its full lifetime, no checkin reset is needed (the engine disposes at
# session end, releasing the underlying connection).
_SET_RLS_SQL = "SELECT set_config('app.current_user_id', %s, false)"


def make_session_rls_engine(url: str, *, user_id: str) -> Engine:
    """Build a sync engine whose connections RLS-scope to ``user_id``.

    Per D-V1-X-rls-engine-shape: one engine per WebRTC session, ``pool_size=1``
    because a session is single-tasked. Unlike the persona-api request-scoped
    pattern (D-08-1, which threads the user_id through a ``ContextVar``
    because connections are shared across concurrent requests), this engine
    is owned by exactly one session so the user_id is baked into the
    checkout listener directly — simpler, equivalently RLS-safe.

    The engine disposes on :meth:`SessionStateMachine.end` — calling
    ``engine.dispose()`` closes the pooled connection, and any in-flight
    transaction rolls back, which is the path that releases the per-user
    ``pg_try_advisory_xact_lock`` (D-V1-5 / D-15-X-concurrency-cap) if the
    session crashed before the lock holder explicitly committed.
    """
    engine = create_engine(url, pool_size=1)

    @event.listens_for(engine, "checkout")
    def _set_rls_on_checkout(
        dbapi_conn: Any,  # noqa: ANN401 — psycopg3 dynamic connection type
        _record: Any,  # noqa: ANN401
        _proxy: Any,  # noqa: ANN401
    ) -> None:
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute(_SET_RLS_SQL, (user_id,))
        finally:
            cursor.close()

    return engine


class SessionStateMachine:
    """Owns one voice session's lifecycle + RLS engine + V4 event dispatch."""

    def __init__(
        self,
        *,
        session_id: str,
        user_id: str,
        persona_id: str,
        conversation_id: str,
        rls_engine: Engine,
        on_event: SessionEventListener | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_id = session_id
        self._user_id = user_id
        self._persona_id = persona_id
        self._conversation_id = conversation_id
        self._rls_engine = rls_engine
        self._on_event = on_event
        self._clock = clock or (lambda: datetime.now(UTC))
        self._state: SessionState = "created"
        self._created_at = self._clock()
        self._ended_at: datetime | None = None
        # Whether SESSION_CREATED has gone out yet. The session record is born in this
        # constructor, which cannot await, so the event goes out at the first async
        # moment the session has: the runner announces it right after construction, and
        # the first transition announces it for anything that did not.
        self._created_announced = False

    # ----- inspection --------------------------------------------------

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def session(self) -> Session:
        """Frozen snapshot of the session at its current state."""
        return Session(
            session_id=self._session_id,
            user_id=self._user_id,
            persona_id=self._persona_id,
            conversation_id=self._conversation_id,
            state=self._state,
            created_at=self._created_at,
            ended_at=self._ended_at,
        )

    @property
    def rls_engine(self) -> Engine:
        """The session-bound RLS engine (T07 audio loop reads from this)."""
        return self._rls_engine

    # ----- state transitions ------------------------------------------

    async def mark_created(self) -> None:
        """Announce the session to the V4 seam. Idempotent (fires at most once).

        Emits :attr:`SessionLifecycleEvent.SESSION_CREATED` with the session still in
        its ``created`` state. The agent runner calls this as soon as it has built the
        session, so a call that dies during connect, before it was ever ``active`` ,
        still leaves a lifecycle trace rather than nothing at all. The two transitions
        below call it too, so the first event a listener ever sees is the create,
        whoever got there first.
        """
        if self._created_announced:
            return
        self._created_announced = True
        await self._dispatch(SessionLifecycleEvent.SESSION_CREATED)

    async def mark_active(self) -> None:
        """``created`` → ``active``. Idempotent (no-op if already active).

        Emits :attr:`SessionLifecycleEvent.SESSION_ACTIVE` to the V4 seam.
        Raises :class:`InvalidSessionStateError` if the session is already
        ``ended`` — a buggy caller cannot revive a closed session.
        """
        if self._state == "active":
            return
        if self._state == "ended":
            msg = "cannot mark_active on an ended session"
            raise InvalidSessionStateError(
                msg, context={"session_id": self._session_id, "state": self._state}
            )
        # Before the flip, so the create is announced with the session as it was created.
        await self.mark_created()
        self._state = "active"
        await self._dispatch(SessionLifecycleEvent.SESSION_ACTIVE)

    async def end(self) -> None:
        """Any state → ``ended``. Idempotent (no-op if already ended).

        Disposes the RLS engine — closes the pooled connection and rolls
        back any in-flight transaction, releasing the per-user advisory
        lock if the session crashed before the lock holder committed.
        Emits :attr:`SessionLifecycleEvent.SESSION_ENDED` LAST so any V4
        cleanup observes the ended state.
        """
        if self._state == "ended":
            return
        await self.mark_created()
        self._state = "ended"

        self._ended_at = self._clock()
        # Engine disposal happens before the event dispatch so listeners see
        # a fully-torn-down state and can't accidentally start a new
        # transaction on a half-disposed engine.
        self._rls_engine.dispose()
        await self._dispatch(SessionLifecycleEvent.SESSION_ENDED)

    # ----- V4 hook dispatch -------------------------------------------

    async def notify(self, ev: SessionLifecycleEvent) -> None:
        """Dispatch a V4 lifecycle event to the registered listener.

        T07's dual-priority queue calls this when VAD / barge-in / turn
        completion events fire. V1 owns the dispatch; V4 owns what happens
        downstream.
        """
        await self._dispatch(ev)

    async def _dispatch(self, ev: SessionLifecycleEvent) -> None:
        if self._on_event is None:
            return
        await self._on_event(ev, self.session)

    # ----- VoiceRoom integration --------------------------------------

    def attach_to_room(self, voice_room: VoiceRoom) -> None:
        """Wire LiveKit Room events to state transitions (T06+T05 bridge).

        On ``Room.on('disconnected')`` — clean or abrupt — the session is
        ended cleanly (engine disposed, advisory lock released, V4 listener
        notified). The room subscription happens via
        :meth:`VoiceRoom.set_disconnect_handler` which spawns the async
        ``end()`` call onto the event loop.
        """

        async def _on_room_disconnected() -> None:
            await self.end()

        voice_room.set_disconnect_handler(_on_room_disconnected)
