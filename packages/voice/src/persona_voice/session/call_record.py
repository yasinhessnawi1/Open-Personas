"""Durable call-record writer (Spec V9, V9-D-5; T3).

A voice call's lifecycle envelope — when it started, when it ended, how long it
ran, and why it stopped — was persisted NOWHERE server-side before V9 (it lived
only in the in-memory :class:`~persona_voice.session.state_machine.Session` and
client storage). :class:`CallRecorder` writes it to the api-owned ``calls``
table, making each finished call a durable, browsable record (the Calls-surface
membership key, V9-D-3).

The voice runtime is **API-free** (``voice → runtime → core``, never
persona-api), so the recorder writes through core's own ``calls`` Table view
(:data:`persona.calls.calls`) on the session's RLS engine — the ``memory_chunks``
P2 precedent, no persona-api import. The owner-scoped RLS ``WITH CHECK`` is
satisfied because that engine sets ``app.current_user_id`` per connection and
``owner_id`` is the session's user.

Both writes are **best-effort** (mirrors the episodic write in
:mod:`persona_voice.model.memory`): they MUST NOT raise — a failed call-record
write degrades the Calls entry, never the call itself. :meth:`open` runs once
when the call goes active; :meth:`close` once at teardown (any reason).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from persona.calls import calls as _calls
from persona.logging import get_logger
from sqlalchemy import insert, update

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine

__all__ = ["CallRecorder", "EndReason"]

#: v1 writes 'disconnect' (clean room end) or 'error' (crash). 'user_hangup' /
#: 'switched' are reserved for a later web-side refinement (the server only sees
#: a room disconnect today). The DB CHECK accepts all four.
EndReason = Literal["user_hangup", "switched", "error", "disconnect"]

_LOG = get_logger("voice.call_record")


class CallRecorder:
    """Writes the durable ``calls`` envelope for one voice call (best-effort).

    Construct once per session over the session's RLS engine + call identity.
    :meth:`open` inserts the in-progress record; :meth:`close` finalizes it with
    ``ended_at`` / ``duration_s`` / ``end_reason``. Both swallow every exception
    (the call lifecycle must never break on a persistence failure).

    Args:
        engine: The session's RLS-scoped sync engine (``app.current_user_id`` set
            per connection, so the owner-scoped RLS ``WITH CHECK`` passes).
        call_id: This call's id (``call_{uuid4().hex}``), the table PK.
        conversation_id: The conversation this call belongs to (shared with chat).
        persona_id: The persona on the call.
        owner_id: The calling user — the RLS anchor.
        clock: UTC-now provider (injected for deterministic tests).
    """

    def __init__(
        self,
        *,
        engine: Engine,
        call_id: str,
        conversation_id: str,
        persona_id: str,
        owner_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._engine = engine
        self._call_id = call_id
        self._conversation_id = conversation_id
        self._persona_id = persona_id
        self._owner_id = owner_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self._started_at: datetime | None = None

    def open(self, started_at: datetime | None = None) -> None:
        """Insert the in-progress call-record (best-effort; MUST NOT raise).

        ``started_at`` defaults to now. Recorded so :meth:`close` can compute the
        stored duration without re-reading the row.
        """
        started = started_at or self._clock()
        self._started_at = started
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    insert(_calls).values(
                        call_id=self._call_id,
                        conversation_id=self._conversation_id,
                        persona_id=self._persona_id,
                        owner_id=self._owner_id,
                        started_at=started,
                    )
                )
        except Exception as exc:  # noqa: BLE001 — call-record persistence is best-effort
            _LOG.warning(
                "call-record open failed (call_id={cid}): {err}",
                cid=self._call_id,
                err=repr(exc)[:300],
            )

    def close(self, *, end_reason: EndReason, ended_at: datetime | None = None) -> int | None:
        """Finalize the call-record with end time, duration, and reason.

        Best-effort; MUST NOT raise (runs in the session teardown's suppressed
        path). ``duration_s`` is STORED (V9-D-5) — computed here from
        ``started_at`` (the :meth:`open` value, or read-back if open was missed).
        A no-op-safe UPDATE: if the row was never inserted (open failed), the
        UPDATE simply matches nothing.

        Returns the computed ``duration_s`` (whole seconds, or ``None`` when the
        start is unknown) so teardown can bill the LiveKit infra tick (Spec M3,
        T6b-1) without re-reading the row — the persistence itself stays best-effort.
        """
        ended = ended_at or self._clock()
        duration_s = self._duration_s(ended)
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    update(_calls)
                    .where(_calls.c.call_id == self._call_id)
                    .values(ended_at=ended, duration_s=duration_s, end_reason=end_reason)
                )
        except Exception as exc:  # noqa: BLE001 — call-record persistence is best-effort
            _LOG.warning(
                "call-record close failed (call_id={cid}): {err}",
                cid=self._call_id,
                err=repr(exc)[:300],
            )
        return duration_s

    def _duration_s(self, ended: datetime) -> int | None:
        """Whole seconds from start to end, or ``None`` if start is unknown."""
        if self._started_at is None:
            return None
        return max(0, int((ended - self._started_at).total_seconds()))
