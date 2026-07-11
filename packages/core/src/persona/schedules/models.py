"""The durable schedule entity + RRULE-class recurrence model (Spec A1, T1).

persona-core owns the schedule contract: a frozen-Pydantic :class:`Schedule`
(an RRULE-class recurring rule **or** a one-time future instant, with the user's
IANA timezone captured on the row), the :class:`RecurrenceRule` that round-trips
to/from an RFC-5545 ``RRULE`` string, the missed-fire policy, and the firing
bookkeeping. No DB, no I/O, no clock — the durable RLS-scoped store and the
single-leader tick (persona-api) compose these. **The next-fire computation is
deliberately NOT here** (T2): this module is the structural entity; T2 is the
pure, DST-correct ``next_fire_after`` over it.

The DST edges live at T2's localization step, never in the rule (D-A1-X-dst-...):
the rule is a pure wall-clock pattern, the timezone is a separate field, and the
gap/fold handling is applied when a naive occurrence is localized to the tz.

See ``docs/specs/phase3/spec_A1/decisions.md`` (D-A1-1 representation, D-A1-2
missed-fire, D-A1-4 tz-on-travel) and ``docs/research/spec_A1.md`` §1.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import rrule as _rrule
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from persona.errors import InvalidRecurrenceRuleError, ScheduleStateError

__all__ = [
    "MissedFirePolicy",
    "RecurrenceFreq",
    "RecurrenceRule",
    "Schedule",
]

# A naive anchor used only to validate that an RRULE string is constructible by
# dateutil (the fail-fast check). It is never a fire time — T2 drives rrule with
# the schedule's real wall-clock dtstart.
_VALIDATION_ANCHOR = datetime(2000, 1, 1, 0, 0)  # noqa: DTZ001 — intentionally naive

# RFC-5545 BYDAY token: optional signed ordinal (e.g. ``1MO``, ``-1FR``) + weekday.
_BYDAY_RE = re.compile(r"^[+-]?\d{0,2}(MO|TU|WE|TH|FR|SA|SU)$")


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalise tz-aware ones to UTC.

    Mirrors the job model + schema-layer rule (spec_01 §11.4): every stored
    timestamp is tz-aware UTC so fires, schedules, and audit times share one
    frame. The schedule's *local* semantics live in its ``timezone`` field, not
    in a naive timestamp.
    """
    if value.tzinfo is None:
        msg = "naive datetime not allowed; use datetime.now(UTC) or attach a tzinfo"
        raise ValueError(msg)
    return value.astimezone(UTC)


class MissedFirePolicy(StrEnum):
    """What the tick does with a fire that came due while the worker was down.

    ``fire-late-once`` catches up with a SINGLE late fire if still within the
    schedule's grace window, then resumes the normal rhythm; ``skip-and-note``
    records the miss and lets the next regular fire proceed. Neither ever
    burst-replays N missed occurrences (criterion 5) — the no-burst invariant is
    structural in T7 (the tick computes the single next due fire, not a backlog).
    """

    FIRE_LATE_ONCE = "fire-late-once"
    SKIP_AND_NOTE = "skip-and-note"


class RecurrenceFreq(StrEnum):
    """The supported RFC-5545 ``FREQ`` values (A1's in-scope expressiveness).

    Sub-daily frequencies are deliberately excluded — the tick is minute-level
    (D-A1-3), and the autonomy surface is daily/weekly/monthly rhythms. An
    unsupported ``FREQ`` is rejected at the boundary, not mis-fired in the tick.
    """

    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"


class RecurrenceRule(BaseModel):
    """An RRULE-class recurrence pattern (frozen). Round-trips to RFC-5545.

    A pure wall-clock pattern: ``byhour``/``byminute`` pin the local time-of-day,
    ``byday``/``bymonthday`` the days, ``interval`` the stride, and ``count``/
    ``until`` the bound (mutually exclusive per RFC-5545). The timezone is NOT
    here — it lives on the :class:`Schedule`, and DST localization is T2's job.
    Validation builds the rule through ``dateutil`` so an unconstructible pattern
    fails loud at the boundary (fail-fast), and ``extra="forbid"`` rejects unknown
    fields.

    Attributes:
        freq: The recurrence frequency (daily/weekly/monthly/yearly).
        interval: Stride between occurrences (every ``interval`` periods).
        byhour: Local hours-of-day the rule fires at (0–23).
        byminute: Local minutes-of-hour the rule fires at (0–59).
        byday: RFC-5545 BYDAY tokens (``MO``..``SU``, optional ordinal like ``1MO``).
        bymonthday: Days of month (1–31, or negative from month end).
        bymonth: Months of year (1–12) — used by a YEARLY rule to pin the month
            (e.g. ``BYMONTH=3;BYMONTHDAY=15`` = every 15 March). Added for A8's
            yearly-on-date vocabulary (A8-D-1); additive + defaulted empty, so every
            pre-A8 serialised rule parses unchanged.
        count: Total number of occurrences before the rule completes (XOR ``until``).
        until: Last instant (tz-aware UTC) the rule may fire at (XOR ``count``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    freq: RecurrenceFreq
    interval: int = Field(default=1, ge=1)
    byhour: tuple[int, ...] = ()
    byminute: tuple[int, ...] = ()
    byday: tuple[str, ...] = ()
    bymonthday: tuple[int, ...] = ()
    bymonth: tuple[int, ...] = ()
    count: int | None = Field(default=None, ge=1)
    until: datetime | None = None

    @field_validator("byhour")
    @classmethod
    def _valid_hours(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(not 0 <= h <= 23 for h in value):
            msg = f"byhour values must be in 0..23, got {value}"
            raise ValueError(msg)
        return value

    @field_validator("byminute")
    @classmethod
    def _valid_minutes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(not 0 <= m <= 59 for m in value):
            msg = f"byminute values must be in 0..59, got {value}"
            raise ValueError(msg)
        return value

    @field_validator("byday")
    @classmethod
    def _valid_byday(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for token in value:
            if not _BYDAY_RE.match(token):
                msg = f"invalid BYDAY token {token!r} (expected e.g. 'MO', '1MO', '-1FR')"
                raise ValueError(msg)
        return value

    @field_validator("bymonthday")
    @classmethod
    def _valid_bymonthday(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(d == 0 or not -31 <= d <= 31 for d in value):
            msg = f"bymonthday values must be in 1..31 or -31..-1, got {value}"
            raise ValueError(msg)
        return value

    @field_validator("bymonth")
    @classmethod
    def _valid_bymonth(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(not 1 <= m <= 12 for m in value):
            msg = f"bymonth values must be in 1..12, got {value}"
            raise ValueError(msg)
        return value

    @field_validator("until")
    @classmethod
    def _until_utc(cls, value: datetime | None) -> datetime | None:
        return _ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _count_until_exclusive(self) -> RecurrenceRule:
        if self.count is not None and self.until is not None:
            msg = "count and until are mutually exclusive (RFC-5545)"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _constructible(self) -> RecurrenceRule:
        # Fail-fast: a rule that dateutil cannot build is malformed. The anchor's
        # awareness must match the rule's UNTIL (RFC-5545: an aware UNTIL requires
        # an aware DTSTART), so a UTC anchor is used when UNTIL is present and a
        # naive one otherwise — the pattern itself is timezone-free, and T2
        # supplies the real wall-clock dtstart at computation time. A ValueError
        # here surfaces as a pydantic ValidationError at the boundary.
        anchor = (
            _VALIDATION_ANCHOR.replace(tzinfo=UTC) if self.until is not None else _VALIDATION_ANCHOR
        )
        try:
            _rrule.rrulestr(self.to_rrule_string(), dtstart=anchor)
        except (ValueError, TypeError) as exc:
            msg = f"unconstructible recurrence rule: {exc}"
            raise ValueError(msg) from exc
        return self

    def to_rrule_string(self) -> str:
        """Serialise to an RFC-5545 ``RRULE`` value (deterministic field order).

        The durable column form and the A4-produces / A6-renders interchange
        shape. ``UNTIL`` is emitted as a UTC ``...Z`` timestamp per RFC-5545.
        """
        parts: list[str] = [f"FREQ={self.freq.value}"]
        if self.interval != 1:
            parts.append(f"INTERVAL={self.interval}")
        if self.byday:
            parts.append(f"BYDAY={','.join(self.byday)}")
        if self.bymonthday:
            parts.append(f"BYMONTHDAY={','.join(str(d) for d in self.bymonthday)}")
        if self.bymonth:
            parts.append(f"BYMONTH={','.join(str(m) for m in self.bymonth)}")
        if self.byhour:
            parts.append(f"BYHOUR={','.join(str(h) for h in self.byhour)}")
        if self.byminute:
            parts.append(f"BYMINUTE={','.join(str(m) for m in self.byminute)}")
        if self.count is not None:
            parts.append(f"COUNT={self.count}")
        if self.until is not None:
            parts.append(f"UNTIL={self.until.strftime('%Y%m%dT%H%M%SZ')}")
        return ";".join(parts)

    @classmethod
    def from_rrule_string(cls, value: str) -> RecurrenceRule:
        """Parse an RFC-5545 ``RRULE`` value into a structured rule.

        Symmetric with :meth:`to_rrule_string`. Raises
        :class:`~persona.errors.InvalidRecurrenceRuleError` (a domain exception,
        not a bare ``ValueError``) on an unparseable string or an unsupported
        ``FREQ`` — this is a direct call path, not a pydantic boundary, so the
        error is a first-class domain type.
        """
        fields: dict[str, str] = {}
        for chunk in value.replace("RRULE:", "", 1).split(";"):
            if not chunk:
                continue
            key, sep, val = chunk.partition("=")
            if not sep:
                raise InvalidRecurrenceRuleError(
                    "malformed RRULE segment",
                    context={"rule": value, "segment": chunk},
                )
            fields[key.strip().upper()] = val.strip()

        raw_freq = fields.get("FREQ")
        if raw_freq is None or raw_freq not in {f.value for f in RecurrenceFreq}:
            raise InvalidRecurrenceRuleError(
                "missing or unsupported FREQ",
                context={"rule": value, "freq": str(raw_freq)},
            )

        def _ints(key: str) -> tuple[int, ...]:
            raw = fields.get(key)
            return tuple(int(p) for p in raw.split(",")) if raw else ()

        until_raw = fields.get("UNTIL")
        until = (
            datetime.strptime(until_raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            if until_raw
            else None
        )
        count_raw = fields.get("COUNT")
        byday = tuple(p for p in fields["BYDAY"].split(",")) if fields.get("BYDAY") else ()
        try:
            return cls(
                freq=RecurrenceFreq(raw_freq),
                interval=int(fields.get("INTERVAL", "1")),
                byhour=_ints("BYHOUR"),
                byminute=_ints("BYMINUTE"),
                byday=byday,
                bymonthday=_ints("BYMONTHDAY"),
                bymonth=_ints("BYMONTH"),
                count=int(count_raw) if count_raw else None,
                until=until,
            )
        except (ValueError, TypeError) as exc:
            raise InvalidRecurrenceRuleError(
                "unparseable RRULE", context={"rule": value, "cause": str(exc)}
            ) from exc


class Schedule(BaseModel):
    """A durable schedule — an RRULE-class recurring rule OR a one-time future.

    Frozen: every change (a fire recorded, a pause, an edit) produces a new
    ``Schedule`` via a copy, never an in-place mutation. Exactly one of
    ``recurrence`` / ``one_time_at`` is set (the XOR). The ``timezone`` is the
    user's captured IANA zone — the localization frame for every fire, stable
    until an explicit edit (D-A1-4, no silent device-following). Firing
    bookkeeping (``last_fire_at`` / ``next_fire_at`` / ``fire_count``) is owned
    by the tick (it computes ``next_fire_at`` via T2's ``next_fire_after``); this
    model only holds and copies it.

    Attributes:
        id: Durable schedule id (the idempotency-key material: ``sched:{id}:{t}``).
        owner_id: The tenant the schedule belongs to (the RLS scope).
        timezone: The captured IANA timezone name (e.g. ``"Europe/Oslo"``).
        recurrence: The recurring rule (XOR ``one_time_at``).
        one_time_at: A single future UTC instant to fire at (XOR ``recurrence``).
        target_job_type: The A0 job type each due fire materialises into.
        payload_template: The payload template merged into each fired job (plus
            the handoff anchor ``schedule_id`` + ``fire_time``, added by the tick).
        enabled: Master on/off; a disabled schedule never fires.
        paused: A soft stop that preserves the rule (resume recomputes next-fire).
        notify_on_fire: Opt-in — when set, a fire writes a coalesced P6 bell
            notification for the owner (one moving entry per schedule, re-alerting
            on each fire). Defaults ``False`` so background/programmatic schedules
            (A4-authored) stay quiet; the user's reminder create door sets it True.
            Preserved across a reschedule (the edit copies from the current row and
            only overrides cadence fields, never this flag).
        missed_fire_policy: How a fire missed during downtime is handled (D-A1-2).
        grace_seconds: Per-schedule grace override (None → the kind-relative
            config default, resolved at tick time in persona-api — D-A1-2).
        last_fire_at: The instant of the most recent materialised fire (UTC).
        next_fire_at: The next due fire (UTC), computed by T2; None when complete.
        fire_count: How many times this schedule has fired.
        created_at: Creation time (UTC).
        updated_at: Last-mutation time (UTC).
        revision: The optimistic-concurrency counter (Spec A8, A8-D-7). Every durable
            mutation bumps it; the store's compare-and-swap writes ``WHERE revision =
            :expected`` so an edit landing between a scheduler fire and its re-arm is
            detected (the mid-flight edit race guard) instead of silently lost. Owned
            by the store like the fire bookkeeping; a fresh schedule starts at 0.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    owner_id: str
    timezone: str
    recurrence: RecurrenceRule | None = None
    one_time_at: datetime | None = None
    target_job_type: str
    # The user/system-defined payload template for fired jobs — intentionally an
    # open JSON object (the tick augments it with the handoff anchor). ``JsonValue``
    # keeps it JSON-shaped without an unbounded ``Any``.
    payload_template: dict[str, JsonValue] = Field(default_factory=dict)
    enabled: bool = True
    paused: bool = False
    notify_on_fire: bool = False
    missed_fire_policy: MissedFirePolicy = MissedFirePolicy.FIRE_LATE_ONCE
    grace_seconds: int | None = Field(default=None, ge=0)
    last_fire_at: datetime | None = None
    next_fire_at: datetime | None = None
    fire_count: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    revision: int = Field(default=0, ge=0)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = f"unknown IANA timezone {value!r}"
            raise ValueError(msg) from exc
        return value

    @field_validator("one_time_at", "last_fire_at", "next_fire_at", "created_at", "updated_at")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _recurrence_xor_one_time(self) -> Schedule:
        has_rule = self.recurrence is not None
        has_one_time = self.one_time_at is not None
        if has_rule == has_one_time:
            msg = "exactly one of recurrence / one_time_at must be set (XOR)"
            raise ValueError(msg)
        return self

    @property
    def is_one_time(self) -> bool:
        """True if this is a one-time future (no recurring rule)."""
        return self.one_time_at is not None

    @property
    def is_active(self) -> bool:
        """True if the schedule may fire — enabled, not paused, and not complete.

        A schedule is complete when its next fire has been exhausted
        (``next_fire_at is None`` after at least one fire, or a one-time that has
        already fired). The tick uses this to decide whether to consider a row.
        """
        if not self.enabled or self.paused:
            return False
        if self.is_one_time:
            return self.fire_count == 0
        return self.next_fire_at is not None or self.fire_count == 0

    @property
    def zoneinfo(self) -> ZoneInfo:
        """The schedule's timezone as a :class:`zoneinfo.ZoneInfo` (the fire frame)."""
        return ZoneInfo(self.timezone)

    def record_fire(self, *, fire_time: datetime, next_fire_at: datetime | None) -> Schedule:
        """Return a copy with this fire recorded (bookkeeping; CQS write helper).

        Increments ``fire_count``, sets ``last_fire_at`` to the fired instant, and
        sets ``next_fire_at`` to the next due fire (``None`` when the rule is
        exhausted or the one-time has fired). ``updated_at`` is advanced to the
        fired instant. Raises :class:`~persona.errors.ScheduleStateError` if a
        one-time schedule has already fired.
        """
        fired = _ensure_utc(fire_time)
        if self.is_one_time and self.fire_count > 0:
            raise ScheduleStateError(
                "one-time schedule already fired",
                context={"schedule_id": self.id, "operation": "record_fire"},
            )
        return self.model_copy(
            update={
                "fire_count": self.fire_count + 1,
                "last_fire_at": fired,
                "next_fire_at": None if self.is_one_time else next_fire_at,
                "updated_at": fired,
            }
        )

    def with_next_fire(self, next_fire_at: datetime | None, *, now: datetime) -> Schedule:
        """Return a copy with ``next_fire_at`` recomputed (edit/resume bookkeeping).

        Used when a rule edit or a resume changes the next due fire without firing.
        ``now`` advances ``updated_at`` (tz-aware UTC).

        Raises :class:`~persona.errors.ScheduleStateError` when ``next_fire_at`` is
        non-``None`` and this schedule is a one-time that has already fired
        (``fire_count > 0``) — R9-023: a one-time is a single-shot fact, so once it
        has fired NOTHING may re-arm it (an edit/reschedule of a fired one-time must
        create a new schedule instead). Setting ``None`` is always allowed (a rule
        completing, or a pause/resume landing on a schedule with no future
        occurrence) — only a non-null RE-ARM on an already-fired one-time is
        illegal. This is the single choke point every ``next_fire_at`` write outside
        of :meth:`record_fire` routes through (``ScheduleStore`` create/edit/resume/
        skip all call this rather than setting the field inline), so no caller —
        present or future — can bypass the guard.
        """
        if next_fire_at is not None and self.is_one_time and self.fire_count > 0:
            raise ScheduleStateError(
                "one-time schedule already fired — create a new schedule",
                context={"schedule_id": self.id, "operation": "with_next_fire"},
            )
        return self.model_copy(
            update={"next_fire_at": next_fire_at, "updated_at": _ensure_utc(now)}
        )
