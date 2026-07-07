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


#: The honest sub-hourly alternative offer (divisors of 60 — the wall-clock minute grids A1
#: can hold). Named in the decline for a non-divisor interval ("every 45 minutes").
_MINUTE_ALTERNATIVE = "every 5, 10, 15, 20, 30 or 60 minutes"
#: The honest hourly alternative offer (divisors of 24 — the wall-clock hour grids).
_HOUR_ALTERNATIVE = "every 1, 2, 3, 4, 6, 8 or 12 hours"


def _unrepresentable(rrule: str, phrase: str, alternative: str | None = None) -> ScheduleParseError:
    context = {"phrase": phrase, "candidate": rrule}
    if alternative is not None:
        context["alternative"] = alternative
    else:
        context["alternative"] = "a fixed daily or weekly time"
    return ScheduleParseError("schedule cannot be represented faithfully", context=context)


def _rrule_fields(rrule: str, *, phrase: str) -> dict[str, str]:
    """Split an RRULE value into an upper-cased key→value map (malformed → parse-honesty)."""
    fields: dict[str, str] = {}
    for chunk in rrule.replace("RRULE:", "", 1).split(";"):
        if not chunk:
            continue
        key, sep, val = chunk.partition("=")
        if not sep:
            raise _unrepresentable(rrule, phrase)
        fields[key.strip().upper()] = val.strip()
    return fields


def _interval_of(fields: dict[str, str], *, rrule: str, phrase: str) -> int:
    try:
        value = int(fields.get("INTERVAL", "1"))
    except ValueError as exc:
        raise _unrepresentable(rrule, phrase) from exc
    if value < 1:
        raise _unrepresentable(rrule, phrase)
    return value


def _with_bound(fields: dict[str, str], base: str) -> str:
    """Re-attach a COUNT/UNTIL bound (occurrence-count semantics survive the rewrite)."""
    parts = [base]
    if fields.get("COUNT"):
        parts.append(f"COUNT={fields['COUNT']}")
    if fields.get("UNTIL"):
        parts.append(f"UNTIL={fields['UNTIL']}")
    return ";".join(parts)


def _normalize_subdaily(rrule: str, *, phrase: str) -> str:
    """Rewrite a sub-daily candidate into the pinned-DAILY wall-clock grid (R4, BUG A).

    Models naturally emit ``FREQ=MINUTELY;INTERVAL=15`` for "every 15 minutes" — but A1's
    :class:`~persona.schedules.RecurrenceFreq` deliberately excludes sub-daily frequencies,
    and before this normalizer the judge silently degraded the ask to a run-once (the R4
    transcript's "once, at 09:19" echo for "every 15 min"). The equivalent wall-clock
    pattern IS A1-expressible whenever the interval divides the clock: every N minutes
    (``60 % N == 0``) becomes ``FREQ=DAILY;BYHOUR=0..23;BYMINUTE=0,N,…``; every N hours
    (``24 % N == 0``) becomes ``FREQ=DAILY;BYHOUR=0,N,…`` — deterministic, dateutil-
    constructible, and fired by the minute-level tick. A NON-divisor interval (every 45
    minutes, every 7 hours) cannot align to a fixed wall-clock grid and raises
    :class:`ScheduleParseError` naming the nearest cadences that CAN be held — never a
    silent fallback (parse-honesty). A given ``BYHOUR`` on a MINUTELY candidate is kept
    (it faithfully restricts the minute grid to those hours). A candidate whose ``FREQ``
    is already daily-or-slower is returned unchanged.
    """
    fields = _rrule_fields(rrule, phrase=phrase)
    freq = fields.get("FREQ", "").upper()
    if freq not in {"SECONDLY", "MINUTELY", "HOURLY"}:
        return rrule
    if freq == "SECONDLY":
        raise _unrepresentable(rrule, phrase, _MINUTE_ALTERNATIVE)
    interval = _interval_of(fields, rrule=rrule, phrase=phrase)
    if freq == "MINUTELY":
        if interval % 60 == 0:  # whole hours stated in minutes ("every 120 minutes")
            freq, interval = "HOURLY", interval // 60
        elif 60 % interval == 0:
            hours = fields.get("BYHOUR") or ",".join(str(h) for h in range(24))
            minutes = ",".join(str(m) for m in range(0, 60, interval))
            return _with_bound(fields, f"FREQ=DAILY;BYHOUR={hours};BYMINUTE={minutes}")
        else:
            raise _unrepresentable(rrule, phrase, _MINUTE_ALTERNATIVE)
    # HOURLY (possibly folded down from a whole-hour MINUTELY above).
    if interval % 24 == 0:  # whole days stated in hours ("every 24/48 hours")
        base = "FREQ=DAILY" if interval == 24 else f"FREQ=DAILY;INTERVAL={interval // 24}"
        if fields.get("BYHOUR"):
            base += f";BYHOUR={fields['BYHOUR']}"
        if fields.get("BYMINUTE"):
            base += f";BYMINUTE={fields['BYMINUTE']}"
        return _with_bound(fields, base)
    if 24 % interval == 0:
        hours = ",".join(str(h) for h in range(0, 24, interval))
        minute = fields.get("BYMINUTE") or "0"
        return _with_bound(fields, f"FREQ=DAILY;BYHOUR={hours};BYMINUTE={minute}")
    raise _unrepresentable(rrule, phrase, _HOUR_ALTERNATIVE)


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
    # R4 (BUG A): a sub-daily candidate ("every 15 minutes" → FREQ=MINUTELY;INTERVAL=15)
    # is normalized to its A1-expressible pinned-DAILY wall-clock grid when the interval
    # divides the clock; a non-divisor declines honestly with the nearest cadences.
    normalized = _normalize_subdaily(rrule, phrase=phrase)
    try:
        rule = RecurrenceRule.from_rrule_string(normalized)
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
