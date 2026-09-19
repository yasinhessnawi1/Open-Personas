"""Unit cover for the occurrences read model: persona resolution + the system-fire exclusion.

No DB. Two things live here.

R9-024: pure-function coverage of
:func:`persona_api.services.occurrences_service._resolved_persona_id`, the two linkage kinds a
schedule can carry a persona through (task-backed ``task_scheduled_fire`` vs. direct-payload
``initiative_scan``), and the no-match cases.

Issue #11: the system-fire exclusion, driven through the REAL :func:`list_occurrences` with the
store, the tasks join and the audit history substituted, so the loop and the filter under test
are the production ones. RLS and the real SQL stay at the integration layer
(``test_r9_024_calendar_chat_panel.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule
from persona_api.config import APIConfig
from persona_api.initiative.handler import INITIATIVE_SCAN_JOB_TYPE
from persona_api.services import occurrences_service
from persona_api.services.occurrences_service import _resolved_persona_id

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
_WINDOW_FROM = datetime(2026, 9, 19, 0, 0, tzinfo=UTC)
_WINDOW_TO = _WINDOW_FROM + timedelta(days=3)


def _schedule(schedule_id: str, *, payload_template: dict[str, object] | None = None) -> Schedule:
    return Schedule(
        id=schedule_id,
        owner_id="owner-1",
        timezone="Europe/Oslo",
        recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(8,), byminute=(0,)),
        target_job_type="task_scheduled_fire",
        payload_template=payload_template or {},
        created_at=_CREATED,
        updated_at=_CREATED,
    )


def test_task_linked_schedule_resolves_through_the_task_join() -> None:
    """A ``task_scheduled_fire`` schedule resolves via ``_task_by_schedule`` — the join
    already computed for every occurrence's own ``task_id``/``persona_id`` fields."""
    schedule = _schedule("sched-1")
    task_by_schedule = {"sched-1": ("task-1", "persona-a")}
    assert _resolved_persona_id(schedule, task_by_schedule) == "persona-a"


def test_payload_linked_schedule_resolves_from_its_own_template() -> None:
    """An ``initiative_scan`` schedule (no backing task) resolves from
    ``payload_template.persona_id`` directly."""
    schedule = _schedule("initsched:persona-b", payload_template={"persona_id": "persona-b"})
    assert _resolved_persona_id(schedule, task_by_schedule={}) == "persona-b"


def test_task_linkage_wins_over_a_stray_payload_persona_id() -> None:
    """Priority order: a real task linkage is authoritative even if the payload also carries
    a (stale/unrelated) persona_id key — the task join is the ground truth for
    ``task_scheduled_fire`` rows."""
    schedule = _schedule("sched-2", payload_template={"persona_id": "wrong-persona"})
    task_by_schedule = {"sched-2": ("task-2", "persona-c")}
    assert _resolved_persona_id(schedule, task_by_schedule) == "persona-c"


def test_no_task_and_no_payload_persona_id_resolves_none() -> None:
    """No linkage at all (neither a task join hit nor a payload persona_id) → None, so it
    never matches ANY persona_id filter (excluded, not wildcarded)."""
    schedule = _schedule("sched-3")
    assert _resolved_persona_id(schedule, task_by_schedule={}) is None


def test_non_string_payload_persona_id_resolves_none_defensively() -> None:
    """A malformed payload (persona_id present but not a string — the JsonValue field is
    open JSON) resolves None rather than a type-unsafe match."""
    schedule = _schedule("sched-4", payload_template={"persona_id": 12345})
    assert _resolved_persona_id(schedule, task_by_schedule={}) is None


# --- the system-originated exclusion (issue #11) -----------------------------
#
# Four personas with initiative on meant four identical "every day at 07:00" cards on a
# calendar the user had never put anything on. These drive the REAL ``list_occurrences``
# (store + join + history substituted, the loop and the filter are the production ones),
# because the filter has to hold for the agenda, week and month views at once: they are
# one server-side read, not three client renderings.


class _FakeStore:
    """Stands in for ``ScheduleStore``; hands back a fixed owner schedule list."""

    def __init__(self, schedules: list[Schedule]) -> None:
        self._schedules = schedules

    def list_for_owner(self, owner_id: str) -> list[Schedule]:  # noqa: ARG002 - RLS is the DB's job
        return list(self._schedules)


def _initiative_schedule(persona_id: str) -> Schedule:
    """What ``ensure_initiative_schedule`` provisions: the persona's own daily wake-up."""
    return Schedule(
        id=f"initsched:{persona_id}",
        owner_id="owner-1",
        timezone="Europe/Oslo",
        recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,), byminute=(0,)),
        target_job_type=INITIATIVE_SCAN_JOB_TYPE,
        payload_template={"persona_id": persona_id},
        created_at=_CREATED,
        updated_at=_CREATED,
    )


def _user_routine(schedule_id: str) -> Schedule:
    """What the "New routine" dialog creates: a task-backed fire the user asked for."""
    return _schedule(schedule_id)


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    schedules: list[Schedule],
    **kwargs: object,
) -> occurrences_service.OccurrencesResult:
    monkeypatch.setattr(occurrences_service, "ScheduleStore", lambda _engine: _FakeStore(schedules))
    monkeypatch.setattr(occurrences_service, "_task_by_schedule", lambda _engine, _owner: {})
    monkeypatch.setattr(occurrences_service, "_fire_history", lambda *_a, **_k: [])
    return occurrences_service.list_occurrences(
        None,  # type: ignore[arg-type] - the store + join are substituted above
        owner_id="owner-1",
        from_=_WINDOW_FROM,
        to=_WINDOW_TO,
        config=APIConfig(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_the_calendar_hides_the_personas_daily_wake_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default read (what the calendar, the chat panel and the digest all use) drops
    every ``initiative_scan`` schedule, however many personas have initiative on."""
    result = _drive(
        monkeypatch,
        [_initiative_schedule(p) for p in ("office-iris", "jarvis", "vegard", "leif")],
    )

    assert result.occurrences == ()
    assert result.cadences == {}  # no reschedule cadence for a row nobody can see either


def test_the_calendar_keeps_the_routines_the_user_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exclusion is a stamp, not a blanket: a user routine at the same hour survives."""
    result = _drive(monkeypatch, [_initiative_schedule("jarvis"), _user_routine("sched-user-1")])

    assert {o.schedule_id for o in result.occurrences} == {"sched-user-1"}
    assert set(result.cadences) == {"sched-user-1"}


def test_a_reopened_calendar_hides_exactly_what_the_live_one_hid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The durable rows are untouched, so a FRESH fetch (reopening the calendar tomorrow,
    or switching agenda -> week -> month) has to answer the same. The filter lives on the
    read path for that reason; nothing about it is client-side or per-session state."""
    schedules = [_initiative_schedule("jarvis"), _user_routine("sched-user-1")]

    live = _drive(monkeypatch, schedules)
    reopened = _drive(monkeypatch, schedules)

    assert reopened.occurrences == live.occurrences
    assert {o.schedule_id for o in reopened.occurrences} == {"sched-user-1"}
    assert len(schedules) == 2  # the wake-up schedule still exists; it is hidden, not deleted


def test_the_persona_can_still_see_its_own_wake_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """``include_system=True`` is the ``schedule_introspect`` scope="mine" read: the persona
    looking at its own commitments, where the wake-up genuinely is one of them."""
    result = _drive(monkeypatch, [_initiative_schedule("jarvis")], include_system=True)

    assert {o.schedule_id for o in result.occurrences} == {"initsched:jarvis"}
    assert result.occurrences[0].persona_id == "jarvis"
