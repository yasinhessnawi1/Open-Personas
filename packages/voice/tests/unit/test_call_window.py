"""The per-call billable window (R9-202).

Proves the two answers teardown needs: when the conversation actually ended
(the caller leaving the room, or failing that the last committed turn), and the
per-call ceiling that bounds whatever is left.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from persona_voice.session.call_window import (
    DEFAULT_MAX_BILLABLE_CALL_S,
    CallBillingWindow,
)

_T0 = datetime(2026, 9, 2, 12, 42, 0, tzinfo=UTC)


class _Clock:
    """A hand-wound UTC clock."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _window(
    clock: _Clock, *, max_billable_s: int = DEFAULT_MAX_BILLABLE_CALL_S
) -> CallBillingWindow:
    return CallBillingWindow(call_id="call_x", max_billable_s=max_billable_s, clock=clock)


class TestConversationEnd:
    def test_no_signal_leaves_the_end_unknown(self) -> None:
        window = _window(_Clock(_T0))
        assert window.conversation_ended_at() is None
        assert window.saw_participant_leave is False

    def test_a_participant_leaving_marks_the_end(self) -> None:
        clock = _Clock(_T0)
        window = _window(clock)
        clock.advance(120)
        window.note_participant_left()
        assert window.conversation_ended_at() == _T0 + timedelta(seconds=120)
        assert window.saw_participant_leave is True

    def test_a_committed_turn_marks_the_end_when_nobody_was_seen_to_leave(self) -> None:
        """The worker-killed case: no room event ever arrives, so the last turn is
        the only evidence of when the conversation stopped."""
        clock = _Clock(_T0)
        window = _window(clock)
        clock.advance(30)
        window.note_turn_committed()
        clock.advance(45)
        window.note_turn_committed()
        assert window.conversation_ended_at() == _T0 + timedelta(seconds=75)
        assert window.saw_participant_leave is False

    def test_the_mark_never_moves_backwards(self) -> None:
        clock = _Clock(_T0)
        window = _window(clock)
        clock.advance(300)
        window.note_turn_committed()
        clock.now = _T0 + timedelta(seconds=10)  # a clock that jumped back
        window.note_participant_left()
        assert window.conversation_ended_at() == _T0 + timedelta(seconds=300)


class TestBillableCeiling:
    def test_a_normal_call_is_billed_in_full(self) -> None:
        window = _window(_Clock(_T0), max_billable_s=7200)
        assert window.billable_seconds(1800) == 1800

    def test_an_over_long_call_is_billed_at_the_ceiling(self) -> None:
        window = _window(_Clock(_T0), max_billable_s=7200)
        # The production shape: eleven hours measured, two hours charged.
        assert window.billable_seconds(40_008) == 7200

    def test_a_zero_ceiling_disables_the_cap(self) -> None:
        window = _window(_Clock(_T0), max_billable_s=0)
        assert window.billable_seconds(40_008) == 40_008

    def test_a_negative_duration_bills_nothing(self) -> None:
        window = _window(_Clock(_T0))
        assert window.billable_seconds(-5) == 0
