"""R9-178 — the reschedule picker opens on the schedule's REAL cadence, not an invented one.

The dialog used to render the builder with no initial value, so every schedule opened as
"Every day, 09:00" and an untouched Apply silently rewrote an hourly schedule into a daily one.
The read side now serves the cadence in the picker vocabulary (``rule_to_pattern``) on both
surfaces the dialog can reach: the task detail (``schedule_cadence``) and the occurrences
result (``cadences``, one per schedule). A rule the picker cannot represent serves neither a
pattern nor a one-time instant: the honest decline the dialog turns into a "Replace" warning.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from persona.errors import ScheduleNotFoundError
from persona.schedules import (
    RecurrenceFreq,
    RecurrenceKind,
    RecurrenceRule,
    Schedule,
)
from persona_api.routes import tasks as tasks_routes
from persona_api.services import occurrences_service
from persona_api.services.calendar_reschedule_service import current_cadence

_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
_HOURLY = RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=tuple(range(24)), byminute=(0,))
_DAILY_0700 = RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,), byminute=(0,))
_UNPICKABLE = RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(1, 5, 9), byminute=(0,))


def _schedule(
    schedule_id: str,
    *,
    recurrence: RecurrenceRule | None = _HOURLY,
    one_time_at: datetime | None = None,
) -> Schedule:
    return Schedule(
        id=schedule_id,
        owner_id="owner-1",
        timezone="Europe/Oslo",
        recurrence=recurrence,
        one_time_at=one_time_at,
        target_job_type="task_scheduled_fire",
        created_at=_NOW,
        updated_at=_NOW,
    )


class _FakeStore:
    """Stands in for ``ScheduleStore``: the RLS read is proven at the integration layer."""

    def __init__(self, *schedules: Schedule) -> None:
        self._by_id = {s.id: s for s in schedules}

    def get(self, owner_id: str, schedule_id: str) -> Schedule:  # noqa: ARG002 — store signature
        try:
            return self._by_id[schedule_id]
        except KeyError as exc:
            raise ScheduleNotFoundError(
                "schedule not found", context={"schedule_id": schedule_id}
            ) from exc

    def list_for_owner(self, owner_id: str) -> list[Schedule]:  # noqa: ARG002 — store signature
        return list(self._by_id.values())


# --- the mapping itself --------------------------------------------------------------------


def test_hourly_schedule_serves_the_hourly_picker_pattern() -> None:
    out = current_cadence(_schedule("s1", recurrence=_HOURLY))
    assert out.pattern is not None
    assert out.pattern.kind is RecurrenceKind.HOURLY
    assert out.pattern.interval == 1
    assert out.one_time_at is None
    assert out.timezone == "Europe/Oslo"
    assert out.human_terms.startswith("every hour")


def test_daily_schedule_serves_its_real_time_not_the_builder_default() -> None:
    out = current_cadence(_schedule("s1", recurrence=_DAILY_0700))
    assert out.pattern is not None
    assert out.pattern.kind is RecurrenceKind.DAILY
    assert (out.pattern.hour, out.pattern.minute) == (7, 0)


def test_one_time_schedule_serves_the_instant_and_no_pattern() -> None:
    at = _NOW + timedelta(days=1)
    out = current_cadence(_schedule("s1", recurrence=None, one_time_at=at))
    assert out.pattern is None
    assert out.one_time_at == at


def test_unpickable_rule_declines_honestly_but_still_reads_as_prose() -> None:
    """Neither a pattern nor an instant: the dialog must say so and label the apply "Replace"."""
    out = current_cadence(_schedule("s1", recurrence=_UNPICKABLE))
    assert out.pattern is None
    assert out.one_time_at is None
    assert out.human_terms  # the prose still tells the user what is set today


# --- the task-detail seam (what TaskReschedule reaches through task-detail) ----------------


def test_task_detail_seam_loads_the_cadence_by_schedule_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks_routes, "ScheduleStore", lambda _engine: _FakeStore(_schedule("s1")))
    out = tasks_routes._schedule_cadence(object(), "owner-1", "s1")  # type: ignore[arg-type]
    assert out is not None
    assert out.pattern is not None
    assert out.pattern.kind is RecurrenceKind.HOURLY


def test_task_detail_seam_is_none_without_a_schedule_or_on_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tasks_routes, "ScheduleStore", lambda _engine: _FakeStore())
    assert tasks_routes._schedule_cadence(object(), "owner-1", None) is None  # type: ignore[arg-type]
    # A dangling schedule_id (deleted row) must not 500 the task detail.
    assert tasks_routes._schedule_cadence(object(), "owner-1", "gone") is None  # type: ignore[arg-type]


# --- the occurrences seam (what the calendar's reschedule dialog reaches) -------------------


def test_occurrences_result_carries_one_cadence_per_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hourly = _schedule("hourly")
    daily = _schedule("daily", recurrence=_DAILY_0700)
    monkeypatch.setattr(
        occurrences_service, "ScheduleStore", lambda _engine: _FakeStore(hourly, daily)
    )
    monkeypatch.setattr(occurrences_service, "_task_by_schedule", lambda _engine, _owner_id: {})
    monkeypatch.setattr(
        occurrences_service, "_fire_history", lambda _engine, _owner_id, **_bounds: []
    )
    config = SimpleNamespace(
        schedule_occurrences_max_count=500, schedule_occurrences_max_horizon_days=7
    )
    result = occurrences_service.list_occurrences(
        object(),  # type: ignore[arg-type]
        owner_id="owner-1",
        from_=_NOW,
        to=_NOW + timedelta(days=1),
        config=config,  # type: ignore[arg-type]
    )
    # Many hourly rows, but the cadence rides once per schedule, keyed for the dialog.
    assert sum(1 for o in result.occurrences if o.schedule_id == "hourly") > 1
    assert set(result.cadences) == {"hourly", "daily"}
    assert result.cadences["hourly"].pattern is not None
    assert result.cadences["hourly"].pattern.kind is RecurrenceKind.HOURLY
    assert result.cadences["daily"].pattern is not None
    assert (result.cadences["daily"].pattern.hour, result.cadences["daily"].pattern.minute) == (
        7,
        0,
    )
