"""Pure-model unit tests for the schedule entity (Spec A1, T1).

The entity contract only — frozen-ness, the recurrence/one-time XOR, the
RRULE round-trip, the validators, the missed-fire enum, and the bookkeeping
copy helpers. The next-fire computation (T2) and its DST suite are separate;
nothing here needs a clock or a database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona.errors import InvalidRecurrenceRuleError, ScheduleStateError
from persona.schedules import MissedFirePolicy, RecurrenceFreq, RecurrenceRule, Schedule
from pydantic import ValidationError

_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _daily_7am() -> RecurrenceRule:
    return RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,), byminute=(0,))


def _schedule(**overrides: object) -> Schedule:
    base: dict[str, object] = {
        "id": "sched-1",
        "owner_id": "user-1",
        "timezone": "Europe/Oslo",
        "recurrence": _daily_7am(),
        "target_job_type": "morning_briefing",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(overrides)
    return Schedule(**base)  # type: ignore[arg-type]


# --- RecurrenceRule: RFC-5545 round-trip ------------------------------------


def test_recurrence_rule_round_trips_through_rrule_string() -> None:
    rule = RecurrenceRule(
        freq=RecurrenceFreq.WEEKLY,
        interval=2,
        byday=("MO", "WE", "FR"),
        byhour=(9,),
        byminute=(30,),
    )
    restored = RecurrenceRule.from_rrule_string(rule.to_rrule_string())
    assert restored == rule


def test_to_rrule_string_emits_canonical_rfc5545() -> None:
    rule = RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,), byminute=(0,))
    assert rule.to_rrule_string() == "FREQ=DAILY;BYHOUR=7;BYMINUTE=0"


def test_to_rrule_string_omits_default_interval() -> None:
    assert "INTERVAL" not in RecurrenceRule(freq=RecurrenceFreq.DAILY).to_rrule_string()
    assert "INTERVAL=3" in RecurrenceRule(freq=RecurrenceFreq.DAILY, interval=3).to_rrule_string()


def test_until_round_trips_as_utc_z() -> None:
    until = datetime(2026, 6, 1, 5, 0, tzinfo=UTC)
    rule = RecurrenceRule(freq=RecurrenceFreq.DAILY, until=until)
    assert rule.to_rrule_string().endswith("UNTIL=20260601T050000Z")
    assert RecurrenceRule.from_rrule_string(rule.to_rrule_string()).until == until


def test_first_monday_of_month_pattern_is_supported() -> None:
    rule = RecurrenceRule(freq=RecurrenceFreq.MONTHLY, byday=("1MO",), byhour=(8,))
    assert "BYDAY=1MO" in rule.to_rrule_string()
    assert RecurrenceRule.from_rrule_string(rule.to_rrule_string()) == rule


# --- RecurrenceRule: validation / fail-fast ---------------------------------


def test_count_and_until_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        RecurrenceRule(freq=RecurrenceFreq.DAILY, count=5, until=datetime(2026, 6, 1, tzinfo=UTC))


@pytest.mark.parametrize("hour", [-1, 24, 99])
def test_byhour_out_of_range_rejected(hour: int) -> None:
    with pytest.raises(ValidationError, match="byhour"):
        RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(hour,))


@pytest.mark.parametrize("minute", [-1, 60])
def test_byminute_out_of_range_rejected(minute: int) -> None:
    with pytest.raises(ValidationError, match="byminute"):
        RecurrenceRule(freq=RecurrenceFreq.DAILY, byminute=(minute,))


@pytest.mark.parametrize("token", ["XX", "8ZZ", "MONDAY", ""])
def test_invalid_byday_token_rejected(token: str) -> None:
    with pytest.raises(ValidationError):
        RecurrenceRule(freq=RecurrenceFreq.DAILY, byday=(token,))


@pytest.mark.parametrize("day", [0, 32, -32])
def test_invalid_bymonthday_rejected(day: int) -> None:
    with pytest.raises(ValidationError):
        RecurrenceRule(freq=RecurrenceFreq.MONTHLY, bymonthday=(day,))


def test_negative_bymonthday_last_day_allowed() -> None:
    rule = RecurrenceRule(freq=RecurrenceFreq.MONTHLY, bymonthday=(-1,))
    assert "BYMONTHDAY=-1" in rule.to_rrule_string()


def test_unsupported_freq_string_raises_domain_error() -> None:
    with pytest.raises(InvalidRecurrenceRuleError, match="FREQ"):
        RecurrenceRule.from_rrule_string("FREQ=HOURLY;BYMINUTE=0")


def test_malformed_rrule_segment_raises_domain_error() -> None:
    with pytest.raises(InvalidRecurrenceRuleError, match="malformed"):
        RecurrenceRule.from_rrule_string("FREQ=DAILY;GARBAGE")


def test_recurrence_rule_is_frozen() -> None:
    rule = _daily_7am()
    with pytest.raises(ValidationError):
        rule.freq = RecurrenceFreq.WEEKLY  # type: ignore[misc]


def test_recurrence_rule_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        RecurrenceRule(freq=RecurrenceFreq.DAILY, bogus=1)  # type: ignore[call-arg]


# --- Schedule: XOR + timezone + tz-awareness --------------------------------


def test_schedule_requires_recurrence_xor_one_time_both_set() -> None:
    with pytest.raises(ValidationError, match="XOR"):
        _schedule(one_time_at=_NOW)  # recurrence is also set by the helper


def test_schedule_requires_recurrence_xor_one_time_neither_set() -> None:
    with pytest.raises(ValidationError, match="XOR"):
        _schedule(recurrence=None)


def test_one_time_schedule_is_valid_without_recurrence() -> None:
    sched = _schedule(recurrence=None, one_time_at=_NOW + timedelta(days=1))
    assert sched.is_one_time
    assert sched.recurrence is None


def test_unknown_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        _schedule(timezone="Mars/Olympus_Mons")


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValidationError):
        _schedule(created_at=datetime(2026, 1, 1, 12, 0))  # noqa: DTZ001 — deliberately naive


def test_non_utc_datetime_normalised_to_utc() -> None:
    from zoneinfo import ZoneInfo

    oslo_noon = datetime(2026, 1, 1, 13, 0, tzinfo=ZoneInfo("Europe/Oslo"))
    sched = _schedule(one_time_at=None, created_at=oslo_noon)
    assert sched.created_at.tzinfo == UTC
    assert sched.created_at == oslo_noon  # same instant, normalised frame


def test_schedule_is_frozen() -> None:
    sched = _schedule()
    with pytest.raises(ValidationError):
        sched.enabled = False  # type: ignore[misc]


def test_zoneinfo_property_returns_captured_zone() -> None:
    assert _schedule().zoneinfo.key == "Europe/Oslo"


# --- Schedule: active / one-time semantics ----------------------------------


def test_disabled_or_paused_schedule_is_not_active() -> None:
    assert not _schedule(enabled=False).is_active
    assert not _schedule(paused=True).is_active


def test_fresh_recurring_schedule_is_active() -> None:
    assert _schedule().is_active


def test_fired_one_time_schedule_is_not_active() -> None:
    sched = _schedule(recurrence=None, one_time_at=_NOW + timedelta(days=1))
    fired = sched.record_fire(fire_time=_NOW + timedelta(days=1), next_fire_at=None)
    assert not fired.is_active


# --- Schedule: bookkeeping copy helpers (CQS, frozen) -----------------------


def test_record_fire_increments_count_and_advances_bookkeeping() -> None:
    sched = _schedule()
    next_due = _NOW + timedelta(days=1)
    fired = sched.record_fire(fire_time=_NOW, next_fire_at=next_due)
    assert fired.fire_count == 1
    assert fired.last_fire_at == _NOW
    assert fired.next_fire_at == next_due
    assert fired.updated_at == _NOW
    # Original is untouched (frozen copy semantics).
    assert sched.fire_count == 0
    assert sched.last_fire_at is None


def test_record_fire_on_one_time_clears_next_fire() -> None:
    sched = _schedule(recurrence=None, one_time_at=_NOW + timedelta(days=1))
    fired = sched.record_fire(fire_time=_NOW + timedelta(days=1), next_fire_at=_NOW)
    assert fired.next_fire_at is None  # one-time never reschedules
    assert fired.fire_count == 1


def test_record_fire_twice_on_one_time_raises_state_error() -> None:
    sched = _schedule(recurrence=None, one_time_at=_NOW + timedelta(days=1))
    fired = sched.record_fire(fire_time=_NOW + timedelta(days=1), next_fire_at=None)
    with pytest.raises(ScheduleStateError, match="already fired"):
        fired.record_fire(fire_time=_NOW + timedelta(days=2), next_fire_at=None)


def test_with_next_fire_updates_without_firing() -> None:
    sched = _schedule()
    later = _NOW + timedelta(hours=2)
    next_due = _NOW + timedelta(days=2)
    updated = sched.with_next_fire(next_due, now=later)
    assert updated.next_fire_at == next_due
    assert updated.updated_at == later
    assert updated.fire_count == 0  # no fire recorded


# --- Schedule: with_next_fire re-arm guard (R9-023) -------------------------


def test_with_next_fire_rejects_rearm_on_fired_one_time() -> None:
    """The origin guard: a fired one-time can never get a non-null next_fire_at again."""
    sched = _schedule(recurrence=None, one_time_at=_NOW + timedelta(days=1))
    fired = sched.record_fire(fire_time=_NOW + timedelta(days=1), next_fire_at=None)
    assert fired.fire_count == 1
    assert fired.next_fire_at is None

    with pytest.raises(ScheduleStateError, match="already fired") as exc_info:
        fired.with_next_fire(_NOW + timedelta(days=5), now=_NOW + timedelta(days=2))

    # The guard's context names the schedule + the operation (structured, D-01-12).
    assert exc_info.value.context["schedule_id"] == fired.id
    assert exc_info.value.context["operation"] == "with_next_fire"


def test_with_next_fire_allows_null_on_fired_one_time() -> None:
    """Terminating (NULL) a fired one-time is always allowed — only a non-null RE-ARM is illegal.

    This is the self-heal path's write shape (R9-023): a corrupted zombie row is
    reconciled by clearing next_fire_at, which must never itself raise.
    """
    sched = _schedule(recurrence=None, one_time_at=_NOW + timedelta(days=1))
    fired = sched.record_fire(fire_time=_NOW + timedelta(days=1), next_fire_at=None)
    healed = fired.with_next_fire(None, now=_NOW + timedelta(days=2))
    assert healed.next_fire_at is None
    assert healed.fire_count == 1  # unchanged — this is not a fire


def test_with_next_fire_allows_non_null_on_never_fired_one_time() -> None:
    """A one-time with fire_count == 0 may still get a next_fire_at set (create/edit path)."""
    sched = _schedule(recurrence=None, one_time_at=_NOW + timedelta(days=1))
    assert sched.fire_count == 0
    updated = sched.with_next_fire(_NOW + timedelta(days=1), now=_NOW)
    assert updated.next_fire_at == _NOW + timedelta(days=1)


def test_with_next_fire_allows_non_null_on_recurring_with_fire_count() -> None:
    """The guard is one-time-specific: a recurring schedule's normal fire_count > 0 is fine."""
    sched = _schedule()  # daily recurrence
    fired = sched.record_fire(fire_time=_NOW, next_fire_at=_NOW + timedelta(days=1))
    assert fired.fire_count == 1
    later_next = _NOW + timedelta(days=2)
    updated = fired.with_next_fire(later_next, now=_NOW + timedelta(days=1, hours=1))
    assert updated.next_fire_at == later_next


def test_missed_fire_policy_default_is_fire_late_once() -> None:
    assert _schedule().missed_fire_policy is MissedFirePolicy.FIRE_LATE_ONCE


def test_missed_fire_policy_values() -> None:
    assert MissedFirePolicy.FIRE_LATE_ONCE.value == "fire-late-once"
    assert MissedFirePolicy.SKIP_AND_NOTE.value == "skip-and-note"
