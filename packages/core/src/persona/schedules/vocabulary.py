"""The humane recurrence vocabulary + the lossless phrase↔rule↔picker mapping (Spec A8, A8-D-1).

Three representations of one cadence, round-trippable for the supported v1 set:

- **rule** — the canonical :class:`~persona.schedules.RecurrenceRule` (RFC-5545 shaped);
- **phrase** — the human-terms string (:func:`render_human_terms`), the ONE renderer both
  the chat echo (A4) and the calendar picker (A8-C) use, so the two surfaces can never
  drift (criterion 9); and
- **picker-state** — :class:`RecurrencePattern`, a structured, **RRULE-free** view the
  calendar's recurrence builder binds to (no raw RRULE ever reaches a user surface).

The v1 vocabulary (A8-D-1): one-time, daily, weekly-on-selected-days (incl. the weekdays
preset), every-N-days/weeks, monthly-by-date (incl. last day), monthly-by-Nth-weekday
(incl. last weekday), every-N-hours (**wall-clock**, A8-D-8), yearly-on-date. Anything
outside the set — arbitrary multi-BY stacks, uneven hour lists — is DECLINED by the picker
(:func:`rule_to_pattern` returns ``None``) yet still renders a phrase and works
conversationally (the A4 parser already declines-honestly on the unrepresentable).

Pure + dependency-free; unit-tests with zero infrastructure. Lives in persona-core so
runtime (echo) and api (occurrences + picker) share the ONE renderer + mapping.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — runtime Pydantic field type (RecurrencePattern.until)
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona.schedules.models import RecurrenceFreq, RecurrenceRule

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

__all__ = [
    "RecurrenceKind",
    "RecurrencePattern",
    "pattern_to_rule",
    "render_human_terms",
    "render_recurrence_terms",
    "rule_to_pattern",
]

# RFC-5545 weekday tokens in week order (the canonical ordering for a pattern's weekdays).
_WEEK_ORDER: tuple[str, ...] = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_WEEKDAYS_PRESET: tuple[str, ...] = ("MO", "TU", "WE", "TH", "FR")
_WEEKEND_PRESET: tuple[str, ...] = ("SA", "SU")
_WEEKDAY_NAME: dict[str, str] = {
    "MO": "Monday",
    "TU": "Tuesday",
    "WE": "Wednesday",
    "TH": "Thursday",
    "FR": "Friday",
    "SA": "Saturday",
    "SU": "Sunday",
}
_MONTH_NAME: tuple[str, ...] = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
_FREQ_STRIDE: dict[RecurrenceFreq, str] = {
    RecurrenceFreq.DAILY: "day",
    RecurrenceFreq.WEEKLY: "week",
    RecurrenceFreq.MONTHLY: "month",
    RecurrenceFreq.YEARLY: "year",
}


class RecurrenceKind(StrEnum):
    """The picker's top-level recurrence categories (the humane vocabulary, A8-D-1)."""

    DAILY = "daily"  # every N days at a time
    WEEKLY = "weekly"  # every N weeks on selected weekdays at a time
    MONTHLY_DAY = "monthly_day"  # every N months on the Nth day (incl. last, -1)
    MONTHLY_WEEKDAY = "monthly_weekday"  # every N months on the Nth weekday (incl. last, -1)
    HOURLY = "hourly"  # every N hours (wall-clock marks; A8-D-8)
    YEARLY = "yearly"  # every year on a month/day at a time


class RecurrencePattern(BaseModel):
    """The RRULE-free picker-state for a recurring cadence (A8-D-1).

    A structured, UI-bindable view of the v1 vocabulary — the calendar's recurrence
    builder reads/writes THIS, never an RRULE string. Round-trips losslessly with
    :class:`~persona.schedules.RecurrenceRule` via :func:`pattern_to_rule` /
    :func:`rule_to_pattern` for the supported set.

    Attributes:
        kind: The recurrence category.
        interval: Stride — days (DAILY), weeks (WEEKLY), months (MONTHLY_*), hours
            (HOURLY, must divide 24 for a clean wall-clock cadence); 1 for YEARLY.
        weekdays: WEEKLY — the selected weekdays (BYDAY tokens ``MO``..``SU``, canonical
            week order, no ordinal). The 5-weekday preset renders "every weekday".
        month_day: MONTHLY_DAY — the day of month (1..31, or -1 for the last day).
        weekday: MONTHLY_WEEKDAY — the weekday token (``MO``..``SU``).
        ordinal: MONTHLY_WEEKDAY — which occurrence (1..4, or -1 for the last).
        month: YEARLY — the month (1..12).
        day_of_month: YEARLY — the day of month (1..31).
        hour: Local hour-of-day (0..23). Required for every kind except HOURLY (whose
            marks come from ``interval``); ``None`` only for HOURLY.
        minute: Local minute-of-hour (0..59).
        count: Bound — total occurrences (XOR ``until``).
        until: Bound — last instant, tz-aware UTC (XOR ``count``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RecurrenceKind
    interval: int = Field(default=1, ge=1)
    weekdays: tuple[str, ...] = ()
    month_day: int | None = None
    weekday: str | None = None
    ordinal: int | None = None
    month: int | None = None
    day_of_month: int | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    count: int | None = Field(default=None, ge=1)
    until: datetime | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> RecurrencePattern:
        """Enforce the per-kind field requirements (fail-fast on an ill-formed pattern)."""
        if self.count is not None and self.until is not None:
            msg = "count and until are mutually exclusive"
            raise ValueError(msg)
        if self.kind is RecurrenceKind.HOURLY:
            if 24 % self.interval != 0:
                msg = f"HOURLY interval must divide 24 for a clean cadence, got {self.interval}"
                raise ValueError(msg)
        elif self.hour is None:
            msg = f"{self.kind} requires an hour-of-day"
            raise ValueError(msg)
        if self.kind is RecurrenceKind.WEEKLY and not self.weekdays:
            msg = "WEEKLY requires at least one weekday"
            raise ValueError(msg)
        if self.kind is RecurrenceKind.WEEKLY:
            if any(d not in _WEEK_ORDER for d in self.weekdays):
                msg = f"invalid weekday token in {self.weekdays}"
                raise ValueError(msg)
            if list(self.weekdays) != [d for d in _WEEK_ORDER if d in self.weekdays]:
                msg = "weekdays must be in canonical week order without duplicates"
                raise ValueError(msg)
        if self.kind is RecurrenceKind.MONTHLY_DAY and (
            self.month_day is None or self.month_day == 0 or not -31 <= self.month_day <= 31
        ):
            msg = f"MONTHLY_DAY requires month_day in 1..31 or -1, got {self.month_day}"
            raise ValueError(msg)
        if self.kind is RecurrenceKind.MONTHLY_WEEKDAY and (
            self.weekday not in _WEEK_ORDER or self.ordinal not in (1, 2, 3, 4, -1)
        ):
            msg = "MONTHLY_WEEKDAY requires a weekday + ordinal in {1,2,3,4,-1}"
            raise ValueError(msg)
        if self.kind is RecurrenceKind.YEARLY and (
            self.month is None
            or not 1 <= self.month <= 12
            or self.day_of_month is None
            or not 1 <= self.day_of_month <= 31
        ):
            msg = "YEARLY requires month in 1..12 and day_of_month in 1..31"
            raise ValueError(msg)
        return self


def pattern_to_rule(pattern: RecurrencePattern) -> RecurrenceRule:
    """Map a picker-state pattern to its canonical :class:`RecurrenceRule` (lossless)."""
    kind = pattern.kind
    count = pattern.count
    until = pattern.until
    byhour: tuple[int, ...] = (pattern.hour,) if pattern.hour is not None else ()
    byminute: tuple[int, ...] = (pattern.minute,)

    if kind is RecurrenceKind.DAILY:
        return RecurrenceRule(
            freq=RecurrenceFreq.DAILY,
            interval=pattern.interval,
            byhour=byhour,
            byminute=byminute,
            count=count,
            until=until,
        )
    if kind is RecurrenceKind.WEEKLY:
        return RecurrenceRule(
            freq=RecurrenceFreq.WEEKLY,
            interval=pattern.interval,
            byday=pattern.weekdays,
            byhour=byhour,
            byminute=byminute,
            count=count,
            until=until,
        )
    if kind is RecurrenceKind.MONTHLY_DAY:
        assert pattern.month_day is not None
        return RecurrenceRule(
            freq=RecurrenceFreq.MONTHLY,
            interval=pattern.interval,
            bymonthday=(pattern.month_day,),
            byhour=byhour,
            byminute=byminute,
            count=count,
            until=until,
        )
    if kind is RecurrenceKind.MONTHLY_WEEKDAY:
        assert pattern.ordinal is not None
        assert pattern.weekday is not None
        return RecurrenceRule(
            freq=RecurrenceFreq.MONTHLY,
            interval=pattern.interval,
            byday=(f"{pattern.ordinal}{pattern.weekday}",),
            byhour=byhour,
            byminute=byminute,
            count=count,
            until=until,
        )
    if kind is RecurrenceKind.HOURLY:
        return RecurrenceRule(
            freq=RecurrenceFreq.DAILY,
            byhour=tuple(range(0, 24, pattern.interval)),
            byminute=(pattern.minute,),
            count=count,
            until=until,
        )
    # YEARLY
    assert pattern.month is not None
    assert pattern.day_of_month is not None
    return RecurrenceRule(
        freq=RecurrenceFreq.YEARLY,
        bymonth=(pattern.month,),
        bymonthday=(pattern.day_of_month,),
        byhour=byhour,
        byminute=byminute,
        count=count,
        until=until,
    )


def rule_to_pattern(rule: RecurrenceRule) -> RecurrencePattern | None:
    """Map a rule back to picker-state, or ``None`` if it is outside the v1 vocabulary.

    A ``None`` result means "not editable in the picker" — the calendar shows the rule
    as read-only human terms and steering it stays conversational (the graceful decline;
    no rule is ever lost, only some are un-pickable).
    """
    count = rule.count
    until = rule.until
    hour = rule.byhour[0] if len(rule.byhour) == 1 else None
    minute = rule.byminute[0] if len(rule.byminute) == 1 else 0
    # A pattern pins exactly one minute; multiple byminute values are un-pickable.
    if len(rule.byminute) > 1:
        return None

    if rule.freq is RecurrenceFreq.DAILY:
        if rule.byday or rule.bymonthday or rule.bymonth:
            return None
        step = _even_hour_step(rule.byhour)
        if step is not None:  # every-N-hours (wall-clock marks)
            return RecurrencePattern(
                kind=RecurrenceKind.HOURLY, interval=step, minute=minute, count=count, until=until
            )
        if len(rule.byhour) > 1:  # an irregular multi-hour list is un-pickable
            return None
        return RecurrencePattern(
            kind=RecurrenceKind.DAILY,
            interval=rule.interval,
            hour=hour,
            minute=minute,
            count=count,
            until=until,
        )

    if rule.freq is RecurrenceFreq.WEEKLY:
        if rule.bymonthday or rule.bymonth or len(rule.byhour) > 1:
            return None
        if any(_has_ordinal(tok) or tok not in _WEEK_ORDER for tok in rule.byday):
            return None  # ordinal weekdays are a monthly shape, not a weekly one
        if not rule.byday:
            return None
        weekdays = tuple(d for d in _WEEK_ORDER if d in rule.byday)
        return RecurrencePattern(
            kind=RecurrenceKind.WEEKLY,
            interval=rule.interval,
            weekdays=weekdays,
            hour=hour,
            minute=minute,
            count=count,
            until=until,
        )

    if rule.freq is RecurrenceFreq.MONTHLY:
        if rule.bymonth or len(rule.byhour) > 1 or hour is None:
            return None
        if len(rule.bymonthday) == 1 and not rule.byday:
            return RecurrencePattern(
                kind=RecurrenceKind.MONTHLY_DAY,
                interval=rule.interval,
                month_day=rule.bymonthday[0],
                hour=hour,
                minute=minute,
                count=count,
                until=until,
            )
        if len(rule.byday) == 1 and not rule.bymonthday and _has_ordinal(rule.byday[0]):
            ordinal, weekday = _split_ordinal(rule.byday[0])
            if ordinal not in (1, 2, 3, 4, -1):
                return None
            return RecurrencePattern(
                kind=RecurrenceKind.MONTHLY_WEEKDAY,
                interval=rule.interval,
                weekday=weekday,
                ordinal=ordinal,
                hour=hour,
                minute=minute,
                count=count,
                until=until,
            )
        return None

    # YEARLY
    if len(rule.bymonth) == 1 and len(rule.bymonthday) == 1 and not rule.byday and hour is not None:
        return RecurrencePattern(
            kind=RecurrenceKind.YEARLY,
            month=rule.bymonth[0],
            day_of_month=rule.bymonthday[0],
            hour=hour,
            minute=minute,
            count=count,
            until=until,
        )
    return None


# --- rendering (the ONE human-terms renderer, shared by echo + picker) --------


def render_human_terms(
    *,
    recurrence: RecurrenceRule | None = None,
    one_time_at: datetime | None = None,
    timezone: str,
    zone: ZoneInfo | None = None,
) -> str:
    """Echo a cadence in human terms — accurate, never an approximation (A4-D-1, A8-D-1).

    Exactly one of ``recurrence`` / ``one_time_at`` is rendered. The ONE renderer both the
    A4 chat echo and the A8 calendar picker use (no drift). Times are local wall-clock in
    ``timezone`` (suffixed "your time"), so an "every N hours" cadence reads as wall-clock
    local marks, never an elapsed-time promise (A8-D-8).
    """
    from zoneinfo import ZoneInfo as _ZoneInfo

    if (recurrence is None) == (one_time_at is None):
        msg = "render_human_terms takes exactly one of recurrence or one_time_at"
        raise ValueError(msg)
    if one_time_at is not None:
        local = one_time_at.astimezone(zone or _ZoneInfo(timezone))
        return f"once, on {local:%A %d %B} at {local:%H:%M} your time"
    assert recurrence is not None
    return render_recurrence_terms(recurrence)


def render_recurrence_terms(rule: RecurrenceRule) -> str:
    """Render a recurring rule's cadence in human terms (no raw RRULE, ever)."""
    if rule.freq is RecurrenceFreq.DAILY and not rule.byday and not rule.bymonthday:
        # every-N-minutes (wall-clock minute grid — R4, BUG A): the pinned-DAILY form
        # ("every 15 minutes" → BYHOUR=0..23 × BYMINUTE=0,15,30,45) is rendered as the
        # cadence it IS, with the daily fire VOLUME stated so confirming it is informed
        # consent ("— 96 times a day"), never a 96-mark list.
        minute_step = _even_minute_step(rule.byminute)
        if minute_step is not None and tuple(rule.byhour) == tuple(range(24)):
            per_day = 24 * len(rule.byminute)
            base = (
                f"every {minute_step} minutes, around the clock, {per_day} times a day, your time"
            )
            return _with_bound(base, rule)
        # every-N-hours (wall-clock) — name the interval AND the local marks (A8-D-8), so it
        # can never read as elapsed-time-under-DST.
        step = _even_hour_step(rule.byhour)
        if step is not None:
            minute = rule.byminute[0] if rule.byminute else 0
            marks = _join([f"{h:02d}:{minute:02d}" for h in range(0, 24, step)])
            base = f"every {step} hours, at {marks} your time"
            return _with_bound(base, rule)

    if rule.freq is RecurrenceFreq.YEARLY and rule.bymonth and rule.bymonthday:
        month = _MONTH_NAME[rule.bymonth[0] - 1]
        base = f"every year on {rule.bymonthday[0]} {month}"
        time_phrase = _time_phrase(rule)
        if time_phrase:
            base = f"{base} at {time_phrase} your time"
        return _with_bound(base, rule)

    stride = _FREQ_STRIDE[rule.freq]
    cadence = f"every {stride}" if rule.interval == 1 else f"every {rule.interval} {stride}s"
    day_phrase = _day_phrase(rule)
    time_phrase = _time_phrase(rule)

    parts = [cadence]
    if day_phrase:
        if rule.freq is RecurrenceFreq.WEEKLY and rule.interval == 1:
            parts = [f"every {day_phrase}"]
        else:
            parts.append(f"on {day_phrase}")
    if time_phrase:
        parts.append(f"at {time_phrase} your time")
    return _with_bound(" ".join(parts), rule)


def _day_phrase(rule: RecurrenceRule) -> str:
    """The day-of-week / day-of-month phrase, or '' when the rule pins no day."""
    if rule.byday:
        tokens = tuple(rule.byday)
        if tokens == _WEEKDAYS_PRESET:
            return "weekday"
        if tokens == _WEEKEND_PRESET:
            return "weekend"
        names = [_byday_name(token) for token in tokens]
        return _join(names)
    if rule.bymonthday:
        return _join([_monthday_phrase(d) for d in rule.bymonthday]) + " of the month"
    return ""


def _byday_name(token: str) -> str:
    """A BYDAY token to a human name: 'MO' → 'Monday', '2TU' → 'the 2nd Tuesday'."""
    if _has_ordinal(token):
        ordinal, weekday = _split_ordinal(token)
        return f"the {_ordinal_word(ordinal)} {_WEEKDAY_NAME[weekday]}"
    return _WEEKDAY_NAME.get(token, token)


def _monthday_phrase(day: int) -> str:
    """A BYMONTHDAY value to a human phrase: 5 → 'day 5', -1 → 'the last day'."""
    if day < 0:
        return "the last day" if day == -1 else f"the {_ordinal_word(day)} day"
    return f"day {day}"


def _ordinal_word(n: int) -> str:
    """A signed ordinal to words: 1→'1st', 2→'2nd', -1→'last', -2→'2nd-to-last'."""
    if n == -1:
        return "last"
    if n < 0:
        return f"{_ordinal_word(-n)}-to-last"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n if n < 20 else n % 10, "th")
    return f"{n}{suffix}"


def _time_phrase(rule: RecurrenceRule) -> str:
    """The time-of-day phrase ('07:00', '07:00 and 19:00'), or '' when unpinned.

    A multi-minute grid renders the full hour×minute cross product (the wall-clock marks
    the rule actually fires at — R4 honesty; dateutil expands BYHOUR×BYMINUTE the same way).
    """
    if not rule.byhour:
        return ""
    minutes = rule.byminute or (0,)
    return _join([f"{hour:02d}:{minute:02d}" for hour in rule.byhour for minute in minutes])


def _with_bound(base: str, rule: RecurrenceRule) -> str:
    """Append a trailing '(N times)' / '(until DATE)' bound phrase when present."""
    if rule.count is not None:
        bound = "once" if rule.count == 1 else f"{rule.count} times"
    elif rule.until is not None:
        bound = f"until {rule.until:%d %B %Y}"
    else:
        return base
    return f"{base} ({bound})"


def _even_minute_step(byminute: tuple[int, ...]) -> int | None:
    """The step of a clean every-N-minutes mark set (``[0, N, 2N, …]`` covering the hour),
    else None. A single minute (a time-of-day pin) and an irregular set are NOT sub-hourly.
    """
    if len(byminute) < 2 or byminute[0] != 0:
        return None
    step = byminute[1]
    if step == 0 or 60 % step != 0:
        return None
    return step if tuple(byminute) == tuple(range(0, 60, step)) else None


def _even_hour_step(byhour: tuple[int, ...]) -> int | None:
    """The step of a clean every-N-hours mark set (``[0, N, 2N, …]`` covering 24h), else None.

    A single hour (a daily time-of-day) and an empty/irregular set are NOT hourly.
    """
    if len(byhour) < 2 or byhour[0] != 0:
        return None
    step = byhour[1]
    if step == 0 or 24 % step != 0:
        return None
    return step if tuple(byhour) == tuple(range(0, 24, step)) else None


def _has_ordinal(token: str) -> bool:
    """Whether a BYDAY token carries an ordinal prefix ('2TU', '-1FR' → True; 'MO' → False)."""
    return len(token) > 2


def _split_ordinal(token: str) -> tuple[int, str]:
    """Split an ordinal BYDAY token: '2TU' → (2, 'TU'), '-1FR' → (-1, 'FR')."""
    return int(token[:-2]), token[-2:]


def _join(names: list[str]) -> str:
    """Join names as 'a', 'a and b', or 'a, b and c'."""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])} and {names[-1]}"
