"""Schedule parsing + parse-honesty + human-terms rendering (Spec A4, T2).

A1 stores schedule *structure* only; A4 owns turning a schedule the conversation produced
into an A1-shaped :class:`ParsedSchedule` — and echoing it back in human terms ("every
weekday at 07:00 your time"). The **parse-honesty** boundary lives here (criterion 4): a
candidate that cannot form a valid :class:`persona.schedules.RecurrenceRule` / one-time
instant, or names an unknown timezone, raises :class:`ScheduleParseError` so the persona
declines plainly and offers an alternative — never a silent approximation.

The natural-language → candidate step is the model's (recognition, T4); this module is the
deterministic validator + renderer the model's candidate passes through, so the honesty gate
and the echo are both pure and testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from persona.errors import InvalidRecurrenceRuleError
from persona.schedules import RecurrenceFreq, RecurrenceRule

from persona_runtime.errors import ScheduleParseError
from persona_runtime.task_origination.draft import ParsedSchedule

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "parse_one_time",
    "parse_recurrence",
    "render_human_terms",
]

_WEEKDAY_NAME: dict[str, str] = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}
_WEEKDAYS: tuple[str, ...] = ("MO", "TU", "WE", "TH", "FR")
_WEEKEND: tuple[str, ...] = ("SA", "SU")
_FREQ_EVERY: dict[RecurrenceFreq, str] = {
    RecurrenceFreq.DAILY: "day",
    RecurrenceFreq.WEEKLY: "week",
    RecurrenceFreq.MONTHLY: "month",
    RecurrenceFreq.YEARLY: "year",
}


def _validate_timezone(timezone: str, *, phrase: str) -> ZoneInfo:
    """Resolve an IANA timezone, or decline honestly (fail-fast, parse-honesty)."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleParseError(
            "unknown timezone",
            context={"timezone": timezone, "phrase": phrase},
        ) from exc


def parse_recurrence(rrule: str, timezone: str, *, phrase: str = "") -> ParsedSchedule:
    """Validate an RFC-5545 RRULE candidate into a :class:`ParsedSchedule`.

    Args:
        rrule: The candidate RRULE string (e.g. ``"FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=7"``).
        timezone: The IANA timezone the cadence is anchored in.
        phrase: The original user phrase, carried into the error context for the decline.

    Returns:
        The parsed, human-rendered schedule.

    Raises:
        ScheduleParseError: The candidate is not a representable rule, or the timezone is
            unknown — the persona declines and offers a fixed alternative (parse-honesty).
    """
    _validate_timezone(timezone, phrase=phrase)
    try:
        rule = RecurrenceRule.from_rrule_string(rrule)
    except InvalidRecurrenceRuleError as exc:
        raise ScheduleParseError(
            "schedule cannot be represented faithfully",
            context={
                "phrase": phrase,
                "candidate": rrule,
                "alternative": "a fixed daily or weekly time",
            },
        ) from exc
    return ParsedSchedule(
        recurrence=rule,
        timezone=timezone,
        human_terms=render_human_terms(recurrence=rule, timezone=timezone),
    )


def parse_one_time(at: datetime, timezone: str, *, phrase: str = "") -> ParsedSchedule:
    """Validate a one-time future instant into a :class:`ParsedSchedule`.

    Args:
        at: The single tz-aware instant the task fires at.
        timezone: The IANA timezone the human-terms rendering localizes to.
        phrase: The original user phrase, carried into the error context.

    Returns:
        The parsed, human-rendered one-time schedule.

    Raises:
        ScheduleParseError: ``at`` is naive, or the timezone is unknown.
    """
    if at.tzinfo is None:
        raise ScheduleParseError(
            "one-time schedule instant must be tz-aware",
            context={"phrase": phrase},
        )
    zone = _validate_timezone(timezone, phrase=phrase)
    return ParsedSchedule(
        one_time_at=at,
        timezone=timezone,
        human_terms=render_human_terms(one_time_at=at, timezone=timezone, zone=zone),
    )


def render_human_terms(
    *,
    recurrence: RecurrenceRule | None = None,
    one_time_at: datetime | None = None,
    timezone: str,
    zone: ZoneInfo | None = None,
) -> str:
    """Echo a cadence in human terms — accurate, never an approximation (A4-D-1).

    Exactly one of ``recurrence`` / ``one_time_at`` is rendered. Times are local wall-clock
    in ``timezone`` (suffixed "your time"); the rendering is deliberately conservative — it
    states only what the rule actually encodes.
    """
    if (recurrence is None) == (one_time_at is None):
        msg = "render_human_terms takes exactly one of recurrence or one_time_at"
        raise ValueError(msg)

    if one_time_at is not None:
        local = one_time_at.astimezone(zone or ZoneInfo(timezone))
        return f"once, on {local:%A %d %B} at {local:%H:%M} your time"

    assert recurrence is not None  # narrowed by the XOR above
    return _render_recurrence(recurrence)


def _render_recurrence(rule: RecurrenceRule) -> str:
    """Render a recurring rule's cadence + day + time in human terms."""
    stride = _FREQ_EVERY[rule.freq]
    cadence = f"every {stride}" if rule.interval == 1 else f"every {rule.interval} {stride}s"

    day_phrase = _day_phrase(rule)
    time_phrase = _time_phrase(rule)

    parts = [cadence]
    if day_phrase:
        # "every week" + "on weekdays" reads better as "every weekday"; collapse the common case.
        if rule.freq is RecurrenceFreq.WEEKLY and rule.interval == 1:
            parts = [f"every {day_phrase}"]
        else:
            parts.append(f"on {day_phrase}")
    if time_phrase:
        parts.append(f"at {time_phrase} your time")

    bound = _bound_phrase(rule)
    rendered = " ".join(parts)
    return f"{rendered} ({bound})" if bound else rendered


def _day_phrase(rule: RecurrenceRule) -> str:
    """The day-of-week / day-of-month phrase, or '' when the rule pins no day."""
    if rule.byday:
        tokens = tuple(rule.byday)
        if tokens == _WEEKDAYS:
            return "weekday"
        if tokens == _WEEKEND:
            return "weekend"
        names = [_WEEKDAY_NAME.get(_strip_ordinal(token), token) for token in tokens]
        return _join_names(names)
    if rule.bymonthday:
        days = ", ".join(str(d) for d in rule.bymonthday)
        return f"day {days} of the month"
    return ""


def _time_phrase(rule: RecurrenceRule) -> str:
    """The time-of-day phrase ("07:00", "07:00 and 19:00"), or '' when unpinned."""
    if not rule.byhour:
        return ""
    minute = rule.byminute[0] if rule.byminute else 0
    times = [f"{hour:02d}:{minute:02d}" for hour in rule.byhour]
    return _join_names(times)


def _bound_phrase(rule: RecurrenceRule) -> str:
    """A trailing bound phrase for a counted/until rule, or '' for an open rule."""
    if rule.count is not None:
        return f"{rule.count} times" if rule.count != 1 else "once"
    if rule.until is not None:
        return f"until {rule.until:%d %B %Y}"
    return ""


def _strip_ordinal(token: str) -> str:
    """Drop an RFC-5545 BYDAY ordinal prefix ('1MO' → 'MO') for name lookup."""
    return token[-2:]


def _join_names(names: list[str]) -> str:
    """Join names as 'a', 'a and b', or 'a, b and c'."""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])} and {names[-1]}"
