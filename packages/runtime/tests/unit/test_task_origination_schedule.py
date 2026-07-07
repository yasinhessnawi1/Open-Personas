"""Unit tests for schedule parsing + parse-honesty + human-terms (Spec A4, T2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona_runtime.errors import ScheduleParseError
from persona_runtime.task_origination import (
    parse_one_time,
    parse_recurrence,
    render_human_terms,
)


def test_parse_recurrence_valid_weekday_morning() -> None:
    parsed = parse_recurrence("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7", "Europe/Oslo")
    assert parsed.recurrence is not None
    assert parsed.one_time_at is None
    assert parsed.timezone == "Europe/Oslo"
    assert parsed.human_terms == "every weekday at 07:00 your time"


def test_parse_recurrence_daily_at_nine() -> None:
    parsed = parse_recurrence("FREQ=DAILY;BYHOUR=9", "Europe/Oslo")
    assert parsed.human_terms == "every day at 09:00 your time"


def test_parse_recurrence_unrepresentable_declines_with_alternative() -> None:
    # An invalid / unsupported RRULE candidate must decline honestly, not approximate.
    with pytest.raises(ScheduleParseError) as exc:
        parse_recurrence("FREQ=WHENEVER", "Europe/Oslo", phrase="whenever the weather's nice")
    assert exc.value.context.get("phrase") == "whenever the weather's nice"
    assert "alternative" in exc.value.context


def test_parse_recurrence_unknown_timezone_declines() -> None:
    with pytest.raises(ScheduleParseError):
        parse_recurrence("FREQ=DAILY;BYHOUR=7", "Mars/Olympus_Mons")


def test_parse_one_time_renders_localized() -> None:
    parsed = parse_one_time(datetime(2026, 7, 1, 5, 0, tzinfo=UTC), "Europe/Oslo")
    assert parsed.one_time_at is not None
    assert parsed.recurrence is None
    # 05:00Z is 07:00 in Oslo (CEST, +02:00) in July.
    assert "07:00 your time" in parsed.human_terms
    assert "once," in parsed.human_terms


def test_parse_one_time_rejects_naive() -> None:
    with pytest.raises(ScheduleParseError):
        parse_one_time(datetime(2026, 7, 1, 9), "Europe/Oslo")  # noqa: DTZ001 — naive on purpose


def test_render_weekend_phrase() -> None:
    rule = RecurrenceRule(freq=RecurrenceFreq.WEEKLY, byday=("SA", "SU"), byhour=(10,))
    assert render_human_terms(recurrence=rule, timezone="Europe/Oslo") == (
        "every weekend at 10:00 your time"
    )


def test_render_specific_weekdays() -> None:
    rule = RecurrenceRule(freq=RecurrenceFreq.WEEKLY, byday=("MO", "WE"), byhour=(8,))
    assert render_human_terms(recurrence=rule, timezone="Europe/Oslo") == (
        "every Monday and Wednesday at 08:00 your time"
    )


def test_render_interval_stride() -> None:
    rule = RecurrenceRule(freq=RecurrenceFreq.DAILY, interval=3, byhour=(6,))
    assert render_human_terms(recurrence=rule, timezone="Europe/Oslo") == (
        "every 3 days at 06:00 your time"
    )


def test_render_multiple_times() -> None:
    rule = RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7, 19))
    assert render_human_terms(recurrence=rule, timezone="Europe/Oslo") == (
        "every day at 07:00 and 19:00 your time"
    )


def test_render_count_bound() -> None:
    rule = RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,), count=5)
    rendered = render_human_terms(recurrence=rule, timezone="Europe/Oslo")
    assert rendered == "every day at 07:00 your time (5 times)"


def test_render_requires_exactly_one_kind() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        render_human_terms(timezone="Europe/Oslo")


# --- sub-daily divisor normalization (R4, BUG A): "every 15 min" is now expressible ------


def test_minutely_divisor_normalizes_to_the_pinned_daily_grid() -> None:
    """The transcript ask: FREQ=MINUTELY;INTERVAL=15 → BYHOUR=0..23 × BYMINUTE=0,15,30,45,
    rendered with the honest daily volume (informed consent — never a silent run-once)."""
    parsed = parse_recurrence("FREQ=MINUTELY;INTERVAL=15", "Europe/Oslo", phrase="every 15 min")
    assert parsed.recurrence is not None
    assert parsed.recurrence.freq is RecurrenceFreq.DAILY
    assert parsed.recurrence.byhour == tuple(range(24))
    assert parsed.recurrence.byminute == (0, 15, 30, 45)
    assert parsed.human_terms == ("every 15 minutes, around the clock — 96 times a day, your time")


def test_hourly_divisor_normalizes_to_pinned_hours() -> None:
    parsed = parse_recurrence("FREQ=HOURLY;INTERVAL=2", "Europe/Oslo", phrase="every 2 hours")
    assert parsed.recurrence is not None
    assert parsed.recurrence.byhour == tuple(range(0, 24, 2))
    assert parsed.human_terms.startswith("every 2 hours, at 00:00, 02:00")


def test_whole_hour_minutely_folds_to_hours_and_whole_day_hourly_to_daily() -> None:
    assert parse_recurrence("FREQ=MINUTELY;INTERVAL=120", "Europe/Oslo").human_terms.startswith(
        "every 2 hours"
    )
    daily = parse_recurrence("FREQ=HOURLY;INTERVAL=24", "Europe/Oslo")
    assert daily.recurrence is not None
    assert daily.recurrence.freq is RecurrenceFreq.DAILY
    assert daily.recurrence.interval == 1


def test_minutely_keeps_a_given_byhour_restriction() -> None:
    # "every 15 minutes during hour 9" — the hour restriction is faithful, keep it.
    parsed = parse_recurrence("FREQ=MINUTELY;INTERVAL=15;BYHOUR=9", "Europe/Oslo")
    assert parsed.recurrence is not None
    assert parsed.recurrence.byhour == (9,)
    assert parsed.recurrence.byminute == (0, 15, 30, 45)


def test_minutely_carries_a_count_bound_through_the_rewrite() -> None:
    parsed = parse_recurrence("FREQ=MINUTELY;INTERVAL=15;COUNT=8", "Europe/Oslo")
    assert parsed.recurrence is not None
    assert parsed.recurrence.count == 8
    assert "(8 times)" in parsed.human_terms


def test_non_divisor_minutes_decline_with_the_nearest_cadences() -> None:
    """NEVER a silent fallback: every-45-minutes cannot align to a wall-clock grid."""
    with pytest.raises(ScheduleParseError) as exc:
        parse_recurrence("FREQ=MINUTELY;INTERVAL=45", "Europe/Oslo", phrase="every 45 min")
    assert exc.value.context["alternative"] == "every 5, 10, 15, 20, 30 or 60 minutes"
    assert exc.value.context["phrase"] == "every 45 min"


def test_non_divisor_hours_decline_with_the_nearest_cadences() -> None:
    with pytest.raises(ScheduleParseError) as exc:
        parse_recurrence("FREQ=HOURLY;INTERVAL=7", "Europe/Oslo", phrase="every 7 hours")
    assert exc.value.context["alternative"] == "every 1, 2, 3, 4, 6, 8 or 12 hours"


def test_secondly_always_declines() -> None:
    with pytest.raises(ScheduleParseError):
        parse_recurrence("FREQ=SECONDLY;INTERVAL=30", "Europe/Oslo")
