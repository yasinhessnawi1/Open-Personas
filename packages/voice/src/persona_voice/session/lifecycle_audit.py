"""The production consumer of the voice session lifecycle seam (R9-184).

:class:`~persona_voice.session.state_machine.SessionStateMachine` has always
produced three lifecycle events (``SESSION_CREATED``, ``SESSION_ACTIVE``,
``SESSION_ENDED``) on the V4 :class:`SessionEventListener` seam, and the agent
runner built the machine with ``on_event=None``, so all three dispatched into
nothing. A producer with no consumer: the events existed, were tested, and could
not reach a durable record in production, which is why nothing ever went red.

**The sink.** The voice runtime already writes every spoken turn through the core
:class:`~persona.audit.AuditLogger` port (the four typed stores in
``persona_voice.agent.runner._build_stores`` each take a
:class:`~persona.audit.JSONLAuditLogger` over the session's ``audit_root``). The
call that carried those turns belongs on the same record, so this listener emits
through that same port rather than opening a fourth one. The shape follows Spec
S1's skill events verbatim: ``store="voice_session"`` is a non-store sentinel the
way ``"skill"`` is, one :class:`~persona.audit.AuditEvent` per lifecycle event,
append-only, ordered by its own tz-aware UTC timestamp. Whichever
:class:`~persona.audit.AuditLogger` implementation is injected decides where the
row lands: the JSONL file per persona by default, ``store_audit_events`` in
Postgres behind ``PERSONA_API_AUDIT_BACKEND=postgres`` (Spec R5).

**Fail-soft.** ``JSONLAuditLogger.emit`` raises :class:`AuditWriteError` on a
write failure, which is right for a store mutation and wrong for a live call, so
every emission here is swallow-and-log, mirroring the episodic write in
:mod:`persona_voice.model.memory` and the S1 skill audit in the runtime loop. A
failed audit write degrades the record, never the call.

**Turn traffic is not lifecycle.** V4's ``UserTurnLifecycleBridge`` pushes the
four speaking events through ``SessionStateMachine.notify()`` onto this SAME
listener, several times a turn. Those are turn-taking signals with their own
instrumentation (the T10 ``VoiceLog`` per-hop latency record); writing them to the
audit record would bury the three events that matter under VAD noise. They are
dropped here by construction, not by configuration.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from persona.audit import AuditAction, AuditEvent
from persona.logging import get_logger
from persona.schema.chunks import WriteSource

from persona_voice.session.state_machine import SessionLifecycleEvent

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.audit import AuditLogger

    from persona_voice.session.state_machine import Session

__all__ = ["SessionLifecycleAuditor"]

_LOG = get_logger("voice.lifecycle_audit")

#: The three lifecycle events and the audit action each one becomes. Membership
#: IS the filter: an event absent from this mapping is turn traffic, not lifecycle.
_LIFECYCLE_ACTIONS: Final[dict[SessionLifecycleEvent, AuditAction]] = {
    SessionLifecycleEvent.SESSION_CREATED: AuditAction.SESSION_CREATED,
    SessionLifecycleEvent.SESSION_ACTIVE: AuditAction.SESSION_ACTIVE,
    SessionLifecycleEvent.SESSION_ENDED: AuditAction.SESSION_ENDED,
}


class SessionLifecycleAuditor:
    """Writes the three voice session lifecycle events to the audit record.

    Satisfies the :class:`~persona_voice.session.state_machine.SessionEventListener`
    Protocol structurally, so it plugs into ``SessionStateMachine(on_event=...)``
    with nothing else to wire. One instance per call, constructed at the agent
    runner alongside the session's other audit sinks.

    Args:
        audit_logger: The core audit port the voice runtime already writes its
            turns through. JSONL per persona by default; Postgres
            ``store_audit_events`` when the hosted path selects it (Spec R5).
        clock: UTC-now provider, injected for deterministic tests.
    """

    def __init__(
        self,
        *,
        audit_logger: AuditLogger,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._audit = audit_logger
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __call__(self, event: SessionLifecycleEvent, session: Session) -> None:
        """Record one lifecycle event; drop turn traffic; never raise.

        The ``session`` snapshot is taken by the state machine AFTER the
        transition, so ``metadata["state"]`` is the state the event announces,
        not the one it left.
        """
        action = _LIFECYCLE_ACTIONS.get(event)
        if action is None:
            return
        try:
            self._audit.emit(
                AuditEvent(
                    timestamp=self._clock(),
                    persona_id=session.persona_id,
                    action=action,
                    store="voice_session",
                    # A call's lifecycle is a platform event, not an owner edit
                    # and not something the persona decided (D-01 three sources).
                    source=WriteSource.SYSTEM,
                    # The owner the call is RLS-scoped to: the audit record's
                    # "who", matching the session's RLS engine user.
                    written_by=session.user_id,
                    metadata={
                        "session_id": session.session_id,
                        "conversation_id": session.conversation_id,
                        "state": session.state,
                    },
                )
            )
        except Exception:  # noqa: BLE001: a failed audit write must never break the call
            _LOG.warning(
                "voice lifecycle audit emit failed (session={sid} event={ev})",
                sid=session.session_id,
                ev=event.value,
            )
