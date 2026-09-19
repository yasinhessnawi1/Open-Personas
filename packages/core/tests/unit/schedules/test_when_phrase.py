"""The spoken-time resolver: the words a person uses, turned into one instant.

Every case is anchored on a fixed clock, because "in one hour" is only checkable against a
known now. The declines matter as much as the successes: a phrase this module cannot read
must raise rather than land somewhere plausible, since a schedule at a time nobody meant is
the failure the persona write door exists to avoid.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.errors import InvalidTimezoneError, ScheduleTimePhraseError
from persona.schedules import resolve_when_phrase

#: A Friday lunchtime in Oslo (13:20 local, summer time), chosen so "tomorrow" crosses a day
#: boundary and a bare afternoon clock time is still ahead of now.
_NOW = datetime(2026, 9, 18, 11, 20, 37, tzinfo=UTC)
_OSLO = "Europe/Oslo"


def _resolve(phrase: str, *, now: datetime = _NOW, timezone: str = _OSLO) -> datetime:
    return resolve_when_phrase(phrase, now=now, timezone=timezone)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("in one hour", datetime(2026, 9, 18, 12, 20, tzinfo=UTC)),
        ("in 90 minutes", datetime(2026, 9, 18, 12, 50, tzinfo=UTC)),
        ("in 2 hours", datetime(2026, 9, 18, 13, 20, tzinfo=UTC)),
        ("in three days", datetime(2026, 9, 21, 11, 20, tzinfo=UTC)),
        ("half an hour", datetime(2026, 9, 18, 11, 50, tzinfo=UTC)),
        ("in a week", datetime(2026, 9, 25, 11, 20, tzinfo=UTC)),
    ],
)
def test_relative_offsets_land_on_the_minute(phrase: str, expected: datetime) -> None:
    """Relative phrases are the anchor plus the offset, with the seconds dropped."""
    assert _resolve(phrase) == expected


def test_the_issue_13_phrase_resolves_to_an_hour_from_now() -> None:
    """The exact ask from the reported bug: a Gantt redraw, an hour from now.

    The seconds are dropped deliberately: a one-off at 12:20:37 reads as noise, and the
    flooring is what lets a retried tool call inside the same minute converge on one booking
    instead of two.
    """
    assert _resolve("in one hour") == datetime(2026, 9, 18, 12, 20, tzinfo=UTC)


def test_tomorrow_with_a_clock_time_is_local_wall_clock() -> None:
    """ "tomorrow 09:00" is 09:00 where the USER is, not 09:00 UTC."""
    # Oslo is UTC+2 in September, so 09:00 local is 07:00 UTC the next day.
    assert _resolve("tomorrow 09:00") == datetime(2026, 9, 19, 7, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("tomorrow at 9am", datetime(2026, 9, 19, 7, 0, tzinfo=UTC)),
        ("tomorrow at 5 pm", datetime(2026, 9, 19, 15, 0, tzinfo=UTC)),
        ("today 17:30", datetime(2026, 9, 18, 15, 30, tzinfo=UTC)),
        ("tonight", datetime(2026, 9, 18, 18, 0, tzinfo=UTC)),
        ("tomorrow", datetime(2026, 9, 19, 7, 0, tzinfo=UTC)),
    ],
)
def test_day_words(phrase: str, expected: datetime) -> None:
    """A named day, with or without a clock time, read in the user's own zone."""
    assert _resolve(phrase) == expected


def test_a_bare_clock_time_takes_the_next_time_it_comes_round() -> None:
    """17:30 today is still ahead at 13:20 local, so it stays today."""
    assert _resolve("17:30") == datetime(2026, 9, 18, 15, 30, tzinfo=UTC)


def test_a_bare_clock_time_already_past_rolls_to_tomorrow() -> None:
    """07:00 has gone by 13:20 local, so it means tomorrow's 07:00, never the past."""
    assert _resolve("07:00") == datetime(2026, 9, 19, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("2026-09-19T14:30", datetime(2026, 9, 19, 12, 30, tzinfo=UTC)),
        ("2026-09-19 14:30", datetime(2026, 9, 19, 12, 30, tzinfo=UTC)),
        ("2026-09-19T14:30:00+02:00", datetime(2026, 9, 19, 12, 30, tzinfo=UTC)),
        ("2026-09-19T12:30:00Z", datetime(2026, 9, 19, 12, 30, tzinfo=UTC)),
    ],
)
def test_iso_forms(phrase: str, expected: datetime) -> None:
    """ISO in, the same instant out; a naive value is the user's wall clock, never UTC."""
    assert _resolve(phrase) == expected


def test_a_naive_iso_value_is_not_read_as_utc() -> None:
    """The bug this pins: reading a naive ISO time as UTC silently moves it by the offset."""
    assert _resolve("2026-09-19T14:30") != datetime(2026, 9, 19, 14, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "phrase",
    [
        "",
        "   ",
        "an hour from now",
        "soon",
        "when you get a chance",
        "next tuesday",
        "in 0 hours",
        "in seventeen fortnights",
        "in 5 bananas",
        "25:00",
        "13pm",
    ],
)
def test_unreadable_phrases_decline_rather_than_guess(phrase: str) -> None:
    """The honesty rule: no approximation. A phrase outside the vocabulary raises."""
    with pytest.raises(ScheduleTimePhraseError):
        _resolve(phrase)


def test_the_decline_carries_the_vocabulary_to_offer_back() -> None:
    """The tool turns this into "give me a date and time", so the forms ride the error."""
    with pytest.raises(ScheduleTimePhraseError) as exc:
        _resolve("soon")
    assert "supported" in exc.value.context
    assert "ISO" in exc.value.context["supported"]


def test_a_time_already_gone_is_refused() -> None:
    """A one-off in the past would never fire; say so here, not three layers down."""
    with pytest.raises(ScheduleTimePhraseError, match="past"):
        _resolve("2026-09-17T09:00")


def test_the_same_minute_is_refused_as_past() -> None:
    """Booking for this very minute is not a future commitment; the tick has passed it."""
    with pytest.raises(ScheduleTimePhraseError, match="past"):
        _resolve("2026-09-18T13:20")


def test_beyond_the_horizon_is_refused() -> None:
    """Further out than the calendar reads is a booking nobody could ever see again."""
    with pytest.raises(ScheduleTimePhraseError, match="year"):
        _resolve("2029-01-01T09:00")


def test_an_unknown_timezone_fails_fast() -> None:
    """A zone the system cannot resolve is a boundary error, not a silent UTC fallback."""
    with pytest.raises(InvalidTimezoneError):
        _resolve("in one hour", timezone="Mars/Olympus")


def test_a_relative_offset_crossing_a_dst_change_stays_an_elapsed_hour() -> None:
    """ "In one hour" means 3600 seconds, even across the autumn fold in Oslo.

    Elapsed-time arithmetic on the absolute instant is the right reading of a relative
    phrase: the user means an hour from now, not "the same wall clock plus one".
    """
    # Oslo moves 03:00 back to 02:00 on 2026-10-25 at 01:00 UTC.
    before_fold = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert resolve_when_phrase("in one hour", now=before_fold, timezone=_OSLO) == datetime(
        2026, 10, 25, 1, 30, tzinfo=UTC
    )
