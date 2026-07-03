"""A8 T5 — occurrences_between: forward-stepping the SAME engine path (A8-D-11, criterion 5).

Proves the read-side expansion matches the tick's own next-fire computation exactly (no second
recurrence math), respects the window bounds + cap, and stays DST-correct across a boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from persona.schedules import (
    RecurrenceFreq,
    RecurrenceRule,
    Schedule,
    next_fire_after,
    occurrences_between,
)

_CREATED = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _daily(tz: str, hour: int, **kw: object) -> Schedule:
    return Schedule(
        id="s1",
        owner_id="u1",
        timezone=tz,
        recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(hour,), byminute=(0,), **kw),  # type: ignore[arg-type]
        target_job_type="briefing",
        created_at=_CREATED,
        updated_at=_CREATED,
    )


def test_occurrences_match_repeated_next_fire_after_exactly() -> None:
    """The SAME path: occurrences_between == iterating next_fire_after (criterion 5)."""
    sched = _daily("Europe/Oslo", 8)
    start = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 4, 15, 0, 0, tzinfo=UTC)  # spans the EU spring-forward (29 Mar)
    window = occurrences_between(sched, start, end, cap=1000)

    # Independently walk next_fire_after across the same window (start-1s so an exact
    # start-boundary fire is caught by the >= start filter).
    expected: list[datetime] = []
    nxt = next_fire_after(sched, start - timedelta(seconds=1))
    while nxt is not None and nxt <= end:
        if nxt >= start:
            expected.append(nxt)
        nxt = next_fire_after(sched, nxt)

    assert window == expected
    # DST-correct: every fire reads 08:00 local on both sides of the boundary.
    for fire in window:
        assert fire.astimezone(ZoneInfo("Europe/Oslo")).hour == 8


def test_window_bounds_are_inclusive() -> None:
    sched = _daily("UTC", 9)
    # A window exactly on two fire instants includes both ends.
    start = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    end = datetime(2026, 5, 3, 9, 0, tzinfo=UTC)
    window = occurrences_between(sched, start, end, cap=100)
    assert window[0] == start
    assert window[-1] == end
    assert len(window) == 3  # 1, 2, 3 May at 09:00


def test_cap_bounds_the_result() -> None:
    sched = _daily("UTC", 9)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 12, 31, 0, 0, tzinfo=UTC)  # ~244 daily fires
    window = occurrences_between(sched, start, end, cap=10)
    assert len(window) == 10  # capped, not the full ~244


def test_count_bounded_rule_stops_at_count() -> None:
    sched = _daily("UTC", 9, count=3)  # fires exactly 3 times ever
    window = occurrences_between(
        sched, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC), cap=100
    )
    assert len(window) == 3


def test_until_bounded_rule_stops_at_until() -> None:
    until = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    sched = _daily("UTC", 9, until=until)
    window = occurrences_between(
        sched, datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 12, 31, tzinfo=UTC), cap=100
    )
    assert all(fire <= until for fire in window)
    assert len(window) == 5  # 1..5 Jan at 09:00 (the 5th is <= 12:00 until)


def test_one_time_inside_and_outside_window() -> None:
    instant = datetime(2026, 7, 8, 5, 0, tzinfo=UTC)
    sched = Schedule(
        id="s1",
        owner_id="u1",
        timezone="Europe/Oslo",
        one_time_at=instant,
        target_job_type="briefing",
        created_at=_CREATED,
        updated_at=_CREATED,
    )
    inside = occurrences_between(
        sched, datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 7, 31, tzinfo=UTC), cap=10
    )
    assert inside == [instant]
    outside = occurrences_between(
        sched, datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 31, tzinfo=UTC), cap=10
    )
    assert outside == []


def test_backwards_or_zero_cap_window_is_empty() -> None:
    sched = _daily("UTC", 9)
    backwards = occurrences_between(
        sched, datetime(2026, 5, 3, tzinfo=UTC), datetime(2026, 5, 1, tzinfo=UTC), cap=10
    )
    zero_cap = occurrences_between(
        sched, datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 3, tzinfo=UTC), cap=0
    )
    assert backwards == []
    assert zero_cap == []
