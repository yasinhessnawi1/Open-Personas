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
from persona.schedules import RecurrenceRule, render_human_terms

from persona_runtime.errors import ScheduleParseError
from persona_runtime.task_origination.draft import ParsedSchedule

if TYPE_CHECKING:
    from datetime import datetime

__all__ = [
    "parse_one_time",
    "parse_recurrence",
    "render_human_terms",
]


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
