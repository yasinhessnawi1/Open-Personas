"""How much of a voice call the owner is billed for (R9-202).

A call's wall clock is the lifetime of the session object, and that is not the
conversation. A room that stays open, a worker drain, a redeploy: any of them
lets teardown run hours or days after the last word was spoken, and every one of
those seconds used to reach ``bill_call_infra`` at the per minute LiveKit rate.
Production carried a 16 day "call" and a real $13.34 charge for eleven hours of
an empty room.

This module holds the two answers that stop it:

* :meth:`CallBillingWindow.conversation_ended_at` is when the conversation
  actually ended. The last human participant leaving the room is the primary
  signal (the agent sees ``participant_disconnected`` for its one remote peer);
  the last committed turn is the fallback, for the case where the worker is
  killed before any departure event can arrive. Neither is available on a
  process that dies outright, so the caller falls back to wall clock.
* :meth:`CallBillingWindow.billable_seconds` is the per call sanity ceiling, the
  belt to that brace. It bounds what any future lifecycle bug can cost, and it
  says so in the log with the call id when it binds.

The window observes; it never bills. The stored ``duration_s`` on the call
record is the conversation too (``ended_at - started_at`` on the row keeps the
room's real lifetime recoverable), while the BILLED seconds are this window's
answer after the ceiling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["DEFAULT_MAX_BILLABLE_CALL_S", "CallBillingWindow"]

_LOG = get_logger("voice.call_window")

#: Fallback ceiling (seconds) when no configured one is passed: two hours. Long
#: enough that a genuine long call is billed in full, short enough that a stuck
#: session costs a couple of dollars rather than a wallet.
DEFAULT_MAX_BILLABLE_CALL_S: int = 2 * 60 * 60


class CallBillingWindow:
    """Tracks when a call's conversation ended and caps its billable seconds.

    One per call. The transport feeds :meth:`note_participant_left` from the
    room's ``participant_disconnected`` event; the per turn billing meter feeds
    :meth:`note_turn_committed` on every committed turn. Teardown reads
    :meth:`conversation_ended_at` for the stored duration and
    :meth:`billable_seconds` for the charge.

    Args:
        call_id: This call's id, named in the ceiling log so a clamped call can
            be found in the database.
        max_billable_s: The per call ceiling on billable seconds. ``0`` means no
            ceiling, which is deliberately possible but is not the default.
        clock: UTC now provider, injected for deterministic tests.
    """

    def __init__(
        self,
        *,
        call_id: str,
        max_billable_s: int = DEFAULT_MAX_BILLABLE_CALL_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._call_id = call_id
        self._max_billable_s = max(0, max_billable_s)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_activity_at: datetime | None = None
        self._participant_left = False

    def note_participant_left(self) -> None:
        """Record that a remote participant left the room (the human hung up).

        Calls here are one to one: the agent's only remote peer is the caller, so
        a departure is the end of the conversation. A rejoin followed by more
        turns simply moves the mark forward again.
        """
        self._participant_left = True
        self._advance()

    def note_turn_committed(self) -> None:
        """Record that a turn was committed, the fallback end of conversation mark."""
        self._advance()

    @property
    def saw_participant_leave(self) -> bool:
        """Whether a remote participant was observed leaving this call."""
        return self._participant_left

    def conversation_ended_at(self) -> datetime | None:
        """When the conversation last showed a sign of life, or ``None`` if never.

        ``None`` means neither signal arrived (no turn was ever committed and no
        departure was seen), so the caller has nothing better than wall clock.
        """
        return self._last_activity_at

    def billable_seconds(self, duration_s: int) -> int:
        """Clamp a call's duration to the per call ceiling, loudly.

        Args:
            duration_s: The conversation's duration in whole seconds.

        Returns:
            ``duration_s``, or the ceiling when it is exceeded.
        """
        capped = max(0, duration_s)
        if self._max_billable_s and capped > self._max_billable_s:
            _LOG.warning(
                "voice call billed at the per call ceiling: call_id={cid} "
                "duration_s={dur} ceiling_s={cap}. The conversation was measured "
                "longer than any real call should be, so only the ceiling is "
                "charged. Check the session lifecycle for this call.",
                cid=self._call_id,
                dur=capped,
                cap=self._max_billable_s,
            )
            return self._max_billable_s
        return capped

    def _advance(self) -> None:
        """Move the end of conversation mark to now (monotone, never backwards)."""
        now = self._clock()
        if self._last_activity_at is None or now > self._last_activity_at:
            self._last_activity_at = now
