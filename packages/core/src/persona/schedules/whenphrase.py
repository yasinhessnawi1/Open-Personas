"""Resolve a spoken one-off time into an absolute instant (persona schedule write tools).

A persona asked to "book the Gantt redraw an hour from now" needs one thing the durable
schedule model does not give it: the instant. A1 stores structure, A8 renders it, and the
create door (:mod:`persona_api.services.schedule_create_service`) takes a real
``one_time_at`` datetime. This module is the small deterministic step between the words and
that datetime, so the resolution is pure, testable, and identical every time rather than a
model guessing at arithmetic it is famously bad at.

Two rules make it honest:

* **The clock is injected.** "In one hour" is resolved against the SAME source of truth the
  ``datetime`` built-in tool reads (``datetime.now(UTC)``), passed in as ``now``. There is
  no second clock for the persona to disagree with itself about.
* **An unreadable phrase is an error, never an approximation.** A schedule booked at a time
  nobody meant is worse than a persona that says "give me that as a date and time". The
  supported vocabulary is small and stated; everything else raises
  :class:`~persona.errors.ScheduleTimePhraseError`.

The result is floored to the whole minute. Schedules fire on wall-clock minute grids, a
one-off at 14:32:07.418 reads as noise to the user, and the flooring is what makes the
booking tool's idempotency key stable across a retry within the same minute.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from persona.errors import ScheduleTimePhraseError

__all__ = ["SUPPORTED_WHEN_FORMS", "resolve_when_phrase"]

#: The vocabulary, stated in the words the model is told to use. Kept in one place so the
#: tool description, the decline message and the parser can never drift apart.
SUPPORTED_WHEN_FORMS = (
    "an ISO date and time like 2026-09-19T14:30, "
    "a relative offset like 'in 90 minutes' or 'in 2 hours', "
    "'today' or 'tomorrow' with a clock time like 'tomorrow 09:00', "
    "or a bare clock time like '17:30' (the next time it comes round)"
)

#: Number words a person actually says to a calendar. Anything larger is written as digits.
_WORD_NUMBERS: dict[str, int] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "forty-five": 45,
    "fortyfive": 45,
    "sixty": 60,
    "ninety": 90,
}

_UNIT_MINUTES: dict[str, int] = {
    "min": 1,
    "mins": 1,
    "minute": 1,
    "minutes": 1,
    "hr": 60,
    "hrs": 60,
    "hour": 60,
    "hours": 60,
    "day": 60 * 24,
    "days": 60 * 24,
    "week": 60 * 24 * 7,
    "weeks": 60 * 24 * 7,
}

#: The furthest ahead a one-off may be booked. Matches the calendar's own read horizon, so a
#: booking the persona cannot then read back is refused rather than silently invisible.
_MAX_DAYS_AHEAD = 365

_RELATIVE_RE = re.compile(
    r"^(?:in|after)\s+(?P<amount>[a-z0-9-]+)\s+(?P<unit>[a-z]+)$",
)
_HALF_HOUR_RE = re.compile(r"^(?:in\s+)?(?:a\s+)?half\s+an?\s+hour$")
_DAY_WORD_RE = re.compile(
    r"^(?P<day>today|tonight|tomorrow)(?:\s+(?:at\s+)?(?P<time>.+))?$",
)
_CLOCK_RE = re.compile(
    r"^(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?$",
)

#: What "tonight" means when no clock time is given. Late enough to read as evening, early
#: enough to still be today for most of the day.
_TONIGHT_HOUR = 20
#: What a bare "tomorrow" means when no clock time is given: the start of the working day.
_DEFAULT_HOUR = 9


def resolve_when_phrase(phrase: str, *, now: datetime, timezone: str) -> datetime:
    """Resolve ``phrase`` into the absolute UTC instant it names.

    Args:
        phrase: What the user said, as the persona passed it on: an ISO instant, a relative
            offset, a day word with a clock time, or a bare clock time.
        now: The current instant (tz-aware). The anchor for everything relative; injected so
            the persona's clock and this resolution are the same clock.
        timezone: The IANA zone the phrase is meant in (the user's own zone). A naive ISO
            value and every wall-clock form are read in this zone.

    Returns:
        The instant, tz-aware UTC, floored to the whole minute.

    Raises:
        ScheduleTimePhraseError: If the phrase is empty, outside the supported vocabulary,
            already in the past, or further ahead than the calendar horizon. Never
            approximated: a wrong time is a wrong commitment.
        InvalidTimezoneError: If ``timezone`` is not a resolvable IANA zone.
    """
    zone = _zone(timezone)
    cleaned = " ".join(phrase.strip().lower().split())
    if not cleaned:
        raise _decline("", "I need a time to book it at")
    anchor = now.astimezone(zone)

    resolved = (
        _try_relative(cleaned, now.astimezone(UTC))
        or _try_day_word(cleaned, anchor, zone)
        or _try_clock(cleaned, anchor, zone)
        or _try_iso(cleaned, zone)
    )
    if resolved is None:
        raise _decline(phrase, f"I could not read {phrase!r} as a time")
    return _bounded(resolved, phrase=phrase, now=now)


def _zone(timezone: str) -> ZoneInfo:
    """Resolve the IANA zone, or fail fast (the same boundary rule the schedule model uses)."""
    from persona.errors import InvalidTimezoneError

    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidTimezoneError(
            "invalid timezone (must be an IANA zone)", context={"timezone": timezone}
        ) from exc


def _decline(phrase: str, message: str) -> ScheduleTimePhraseError:
    """The honest decline, carrying the vocabulary the caller should offer back."""
    return ScheduleTimePhraseError(
        message, context={"phrase": phrase, "supported": SUPPORTED_WHEN_FORMS}
    )


def _bounded(resolved: datetime, *, phrase: str, now: datetime) -> datetime:
    """Floor to the minute and refuse a time that is already gone or beyond the horizon."""
    floored = resolved.astimezone(UTC).replace(second=0, microsecond=0)
    if floored <= now.astimezone(UTC).replace(second=0, microsecond=0):
        raise _decline(phrase, f"{phrase!r} is in the past, so nothing would ever fire")
    if floored - now > timedelta(days=_MAX_DAYS_AHEAD):
        raise _decline(phrase, f"{phrase!r} is more than a year out, which is further than I book")
    return floored


def _try_relative(cleaned: str, now_utc: datetime) -> datetime | None:
    """ "in 90 minutes", "in two hours", "in a week", "half an hour".

    The arithmetic is done on the absolute UTC instant, never on the local wall clock.
    Adding a ``timedelta`` to a zoned datetime moves the wall clock, so "in one hour" asked
    half an hour before Oslo's autumn fold would have landed two real hours later. A
    relative phrase means elapsed time, which is what someone asking for "an hour from now"
    is holding in their head.
    """
    if _HALF_HOUR_RE.match(cleaned):
        return now_utc + timedelta(minutes=30)
    match = _RELATIVE_RE.match(cleaned)
    if match is None:
        return None
    minutes_per_unit = _UNIT_MINUTES.get(match.group("unit"))
    if minutes_per_unit is None:
        return None
    amount = _as_count(match.group("amount"))
    if amount is None or amount <= 0:
        return None
    return now_utc + timedelta(minutes=amount * minutes_per_unit)


def _as_count(raw: str) -> int | None:
    """A digit string or one of the number words people actually say."""
    if raw.isdigit():
        return int(raw)
    return _WORD_NUMBERS.get(raw)


def _try_day_word(cleaned: str, anchor: datetime, zone: ZoneInfo) -> datetime | None:
    """ "tomorrow 09:00", "today at 5pm", "tonight": a named day plus an optional clock."""
    match = _DAY_WORD_RE.match(cleaned)
    if match is None:
        return None
    day = match.group("day")
    raw_time = match.group("time")
    if raw_time is None:
        hour, minute = (_TONIGHT_HOUR, 0) if day == "tonight" else (_DEFAULT_HOUR, 0)
    else:
        clock = _parse_clock(raw_time)
        if clock is None:
            return None
        hour, minute = clock
    base = anchor + timedelta(days=1) if day == "tomorrow" else anchor
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=zone)


def _try_clock(cleaned: str, anchor: datetime, zone: ZoneInfo) -> datetime | None:
    """A bare clock time: the next time it comes round (today if still ahead, else tomorrow)."""
    clock = _parse_clock(cleaned)
    if clock is None:
        return None
    hour, minute = clock
    candidate = anchor.replace(hour=hour, minute=minute, second=0, microsecond=0, tzinfo=zone)
    if candidate <= anchor:
        candidate = candidate + timedelta(days=1)
    return candidate


def _parse_clock(raw: str) -> tuple[int, int] | None:
    """ "09:00", "9", "9am", "5 pm", "17:30" → (hour, minute) on a 24 hour clock."""
    match = _CLOCK_RE.match(raw.strip())
    if match is None:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    meridiem = match.group("meridiem")
    if meridiem is not None:
        if not 1 <= hour <= 12:  # noqa: PLR2004 (the 12 hour clock's own bounds)
            return None
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):  # noqa: PLR2004 (clock bounds)
        return None
    return hour, minute


def _try_iso(cleaned: str, zone: ZoneInfo) -> datetime | None:
    """An ISO 8601 instant; a naive value is read in the user's own zone, never UTC."""
    candidate = cleaned.replace(" ", "T", 1) if " " in cleaned else cleaned
    try:
        parsed = datetime.fromisoformat(candidate.upper().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed
