"""R9-024 — unit: the persona-resolution helper behind the chat calendar's ``?persona_id=`` filter.

Pure-function coverage of :func:`persona_api.services.occurrences_service._resolved_persona_id`
(no DB): the two linkage kinds a schedule can carry a persona through (task-backed
``task_scheduled_fire`` vs. direct-payload ``initiative_scan``), and the no-match cases. The
full server-side filter (RLS + real join) is proven at the integration layer
(``test_r9_024_calendar_chat_panel.py``); this file isolates the resolution LOGIC itself.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule
from persona_api.services.occurrences_service import _resolved_persona_id

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


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
