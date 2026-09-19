"""The calendar disclosure ledger: what a user has actually been shown.

It is the only input to the delete gate, so its two failure directions are not symmetric.
Forgetting a disclosure costs one extra calendar read. Inventing one would let a model turn
an id it never saw into a deletion, which is why every test below that could pass by being
generous instead asserts the strict answer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from persona.schedules import ScheduleDisclosureLedger

_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


class _Clock:
    """A hand-wound clock, so expiry is tested by moving time rather than waiting."""

    def __init__(self, now: datetime = _NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def test_an_id_that_was_shown_is_actionable() -> None:
    """The ordinary case: the calendar read listed it, so the removal may proceed."""
    ledger = ScheduleDisclosureLedger(clock=_Clock())
    ledger.record("user_a", ["sch_1", "sch_2"])

    assert ledger.was_shown("user_a", "sch_1") is True
    assert ledger.was_shown("user_a", "sch_2") is True


def test_an_id_nobody_was_shown_is_not_actionable() -> None:
    """The gate itself. A plausible id the model produced is not a disclosure."""
    ledger = ScheduleDisclosureLedger(clock=_Clock())
    ledger.record("user_a", ["sch_1"])

    assert ledger.was_shown("user_a", "sch_invented") is False


def test_one_users_reading_never_licenses_anothers_delete() -> None:
    """Disclosures are per owner, so a shared process cannot cross the wires."""
    ledger = ScheduleDisclosureLedger(clock=_Clock())
    ledger.record("user_a", ["sch_1"])

    assert ledger.was_shown("user_b", "sch_1") is False


def test_a_disclosure_expires() -> None:
    """Last week's calendar reading is not today's authorization."""
    clock = _Clock()
    ledger = ScheduleDisclosureLedger(ttl=timedelta(hours=6), clock=clock)
    ledger.record("user_a", ["sch_1"])

    clock.now = _NOW + timedelta(hours=5, minutes=59)
    assert ledger.was_shown("user_a", "sch_1") is True

    clock.now = _NOW + timedelta(hours=6, seconds=1)
    assert ledger.was_shown("user_a", "sch_1") is False


def test_a_blank_owner_or_id_is_never_a_disclosure() -> None:
    """Off-request there is no owner, and an empty id is not an entry anyone saw."""
    ledger = ScheduleDisclosureLedger(clock=_Clock())
    ledger.record("", ["sch_1"])
    ledger.record("user_a", ["", "sch_2"])

    assert ledger.was_shown("", "sch_1") is False
    assert ledger.was_shown("user_a", "") is False
    assert ledger.was_shown("user_a", "sch_2") is True


def test_the_ledger_stays_bounded() -> None:
    """A long-running process cannot grow this without limit; the oldest go first."""
    clock = _Clock()
    ledger = ScheduleDisclosureLedger(max_entries=10, clock=clock)
    for index in range(25):
        clock.now = _NOW + timedelta(seconds=index)
        ledger.record("user_a", [f"sch_{index}"])

    assert ledger.was_shown("user_a", "sch_0") is False
    assert ledger.was_shown("user_a", "sch_24") is True


def test_the_owner_bound_view_answers_for_its_owner_only() -> None:
    """What the tools actually hold: a view with the owner already fixed."""
    ledger = ScheduleDisclosureLedger(clock=_Clock())
    mine = ledger.bind("user_a")
    theirs = ledger.bind("user_b")
    mine.record(["sch_1"])

    assert mine.was_shown("sch_1") is True
    assert theirs.was_shown("sch_1") is False
