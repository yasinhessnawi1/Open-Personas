"""A8 T2 — the humane recurrence vocabulary + the lossless phrase↔rule↔picker mapping.

The gate bars: (1) the round-trip is lossless across the ENTIRE v1 set (picker-state ↔
rule, property-tested across the vocabulary, not spot-checked); (2) a rule outside the set
declines gracefully (``rule_to_pattern`` → ``None``) yet still renders a phrase; (3) no raw
RRULE reaches any rendered surface; (4) one renderer feeds echo + picker; (5) last-weekday
/ last-day-of-month are in; (6) "every N hours" reads as wall-clock (A8-D-8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import product

import pytest
from persona.schedules import (
    RecurrenceFreq,
    RecurrenceKind,
    RecurrencePattern,
    RecurrenceRule,
    pattern_to_rule,
    render_human_terms,
    render_recurrence_terms,
    rule_to_pattern,
)

_UNTIL = datetime(2027, 1, 1, 0, 0, tzinfo=UTC)

# One representative pattern per vocabulary kind, sans bound (bounds are crossed in below).
_BASE_PATTERNS: list[RecurrencePattern] = [
    RecurrencePattern(kind=RecurrenceKind.DAILY, hour=7, minute=0),
    RecurrencePattern(kind=RecurrenceKind.DAILY, interval=3, hour=9, minute=30),
    RecurrencePattern(kind=RecurrenceKind.WEEKLY, weekdays=("MO",), hour=8),
    RecurrencePattern(kind=RecurrenceKind.WEEKLY, weekdays=("MO", "TU", "WE", "TH", "FR"), hour=8),
    RecurrencePattern(kind=RecurrenceKind.WEEKLY, weekdays=("SA", "SU"), hour=10),
    RecurrencePattern(kind=RecurrenceKind.WEEKLY, weekdays=("MO", "WE", "FR"), interval=2, hour=18),
    RecurrencePattern(kind=RecurrenceKind.MONTHLY_DAY, month_day=1, hour=9),
    RecurrencePattern(kind=RecurrenceKind.MONTHLY_DAY, month_day=15, interval=2, hour=9),
    RecurrencePattern(kind=RecurrenceKind.MONTHLY_DAY, month_day=-1, hour=17),  # last day
    RecurrencePattern(kind=RecurrenceKind.MONTHLY_WEEKDAY, weekday="TU", ordinal=2, hour=8),
    RecurrencePattern(
        kind=RecurrenceKind.MONTHLY_WEEKDAY, weekday="FR", ordinal=-1, hour=16
    ),  # last
    RecurrencePattern(kind=RecurrenceKind.HOURLY, interval=2, minute=0),
    RecurrencePattern(kind=RecurrenceKind.HOURLY, interval=4, minute=30),
    RecurrencePattern(kind=RecurrenceKind.HOURLY, interval=6, minute=0),
    RecurrencePattern(kind=RecurrenceKind.HOURLY, interval=12, minute=0),
    RecurrencePattern(kind=RecurrenceKind.YEARLY, month=3, day_of_month=15, hour=9),
    RecurrencePattern(kind=RecurrenceKind.YEARLY, month=12, day_of_month=31, hour=0, minute=0),
]

# Cross every base pattern with each bound (none / count / until) — the full v1 surface.
_BOUNDS: list[dict[str, object]] = [{}, {"count": 5}, {"until": _UNTIL}]
_ALL_PATTERNS: list[RecurrencePattern] = [
    base.model_copy(update=bound) for base, bound in product(_BASE_PATTERNS, _BOUNDS)
]


@pytest.mark.parametrize("pattern", _ALL_PATTERNS, ids=lambda p: f"{p.kind}-{p.interval}")
def test_pattern_rule_round_trip_is_lossless(pattern: RecurrencePattern) -> None:
    """picker-state → rule → picker-state is the identity across the whole v1 vocabulary."""
    rule = pattern_to_rule(pattern)
    recovered = rule_to_pattern(rule)
    assert recovered == pattern


@pytest.mark.parametrize("pattern", _ALL_PATTERNS, ids=lambda p: f"{p.kind}-{p.interval}")
def test_every_pattern_renders_without_raw_rrule(pattern: RecurrencePattern) -> None:
    """No raw RRULE token ever appears in a rendered phrase (criterion 9)."""
    phrase = render_recurrence_terms(pattern_to_rule(pattern))
    for token in ("FREQ=", "BYDAY", "BYHOUR", "BYMONTHDAY", "BYMONTH", "RRULE", "INTERVAL="):
        assert token not in phrase


# --- graceful decline: rules outside the v1 vocabulary --------------------------------------

_OUT_OF_VOCAB: list[RecurrenceRule] = [
    # An irregular multi-hour list (not an even every-N-hours cadence).
    RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(1, 5, 9), byminute=(0,)),
    # Multiple minutes pinned — the picker holds exactly one.
    RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(9,), byminute=(0, 30)),
    # A monthly rule mixing day-of-month AND weekday — not a single picker shape.
    RecurrenceRule(freq=RecurrenceFreq.MONTHLY, bymonthday=(1,), byday=("MO",), byhour=(9,)),
    # A weekly rule with an ordinal weekday (that is a monthly shape, malformed as weekly).
    RecurrenceRule(freq=RecurrenceFreq.WEEKLY, byday=("2MO",), byhour=(9,)),
    # A 5th-weekday ordinal is outside the picker's {1,2,3,4,last} set.
    RecurrenceRule(freq=RecurrenceFreq.MONTHLY, byday=("5MO",), byhour=(9,)),
]


@pytest.mark.parametrize("rule", _OUT_OF_VOCAB)
def test_out_of_vocabulary_rule_declines_from_picker(rule: RecurrenceRule) -> None:
    """A rule outside v1 → no picker-state (declines), but it STILL renders a phrase."""
    assert rule_to_pattern(rule) is None
    phrase = render_recurrence_terms(rule)
    assert phrase  # non-empty — the calendar can show it read-only; chat still steers it
    assert "FREQ=" not in phrase


# --- rendering specifics (the humane vocabulary, A8-D-1/D-8) ---------------------------------


def test_last_day_of_month_renders_humanely() -> None:
    pattern = RecurrencePattern(kind=RecurrenceKind.MONTHLY_DAY, month_day=-1, hour=17)
    rule = pattern_to_rule(pattern)
    expected = "every month on the last day of the month at 17:00 your time"
    assert render_recurrence_terms(rule) == expected


def test_last_weekday_of_month_renders_humanely() -> None:
    rule = pattern_to_rule(
        RecurrencePattern(kind=RecurrenceKind.MONTHLY_WEEKDAY, weekday="FR", ordinal=-1, hour=16)
    )
    assert render_recurrence_terms(rule) == "every month on the last Friday at 16:00 your time"


def test_nth_weekday_renders_with_ordinal() -> None:
    rule = pattern_to_rule(
        RecurrencePattern(kind=RecurrenceKind.MONTHLY_WEEKDAY, weekday="TU", ordinal=2, hour=8)
    )
    assert render_recurrence_terms(rule) == "every month on the 2nd Tuesday at 08:00 your time"


def test_every_n_hours_reads_as_wall_clock_marks() -> None:
    # A8-D-8: names the local marks + "your time" — never an elapsed-time promise.
    rule = pattern_to_rule(RecurrencePattern(kind=RecurrenceKind.HOURLY, interval=6, minute=0))
    phrase = render_recurrence_terms(rule)
    assert phrase == "every 6 hours, at 00:00, 06:00, 12:00 and 18:00 your time"
    assert "your time" in phrase  # wall-clock-anchored, not elapsed


def test_yearly_on_date_renders() -> None:
    rule = pattern_to_rule(
        RecurrencePattern(kind=RecurrenceKind.YEARLY, month=3, day_of_month=15, hour=9)
    )
    assert render_recurrence_terms(rule) == "every year on 15 March at 09:00 your time"


def test_weekdays_preset_collapses_to_every_weekday() -> None:
    rule = pattern_to_rule(
        RecurrencePattern(
            kind=RecurrenceKind.WEEKLY, weekdays=("MO", "TU", "WE", "TH", "FR"), hour=7
        )
    )
    assert render_recurrence_terms(rule) == "every weekday at 07:00 your time"


def test_bound_renders_count_and_until() -> None:
    counted = pattern_to_rule(RecurrencePattern(kind=RecurrenceKind.DAILY, hour=7, count=5))
    assert render_recurrence_terms(counted) == "every day at 07:00 your time (5 times)"
    until = pattern_to_rule(RecurrencePattern(kind=RecurrenceKind.DAILY, hour=7, until=_UNTIL))
    assert render_recurrence_terms(until).endswith("(until 01 January 2027)")


def test_one_time_renders_in_local_terms() -> None:
    phrase = render_human_terms(
        one_time_at=datetime(2026, 7, 8, 5, 0, tzinfo=UTC), timezone="Europe/Oslo"
    )
    assert phrase == "once, on Wednesday 08 July at 07:00 your time"  # 05:00Z = 07:00 CEST


def test_hourly_interval_must_divide_24() -> None:
    with pytest.raises(ValueError, match="divide 24"):
        RecurrencePattern(kind=RecurrenceKind.HOURLY, interval=5, minute=0)
