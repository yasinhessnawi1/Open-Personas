"""The one source of truth for the schedule-backed-task shape (Spec A10, A10-D-8).

Both creation doors — A4's confirm seam (:mod:`persona_api.services.origination_service`)
and A10's direct user create (:mod:`persona_api.services.schedule_create_service`) — build
their Task + Schedule pair HERE, so the load-bearing invariants cannot fork between doors:

* a schedule-backed task is born ``WAITING(until_time)`` (dormant at zero cost until its
  first scheduled fire resumes it — without this the task sits inert forever);
* the schedule targets the A1→A2 **fire bridge** (``TASK_SCHEDULED_FIRE_JOB_TYPE`` with a
  ``{"task_id": …}`` payload), never ``task_leg`` directly (the leg needs the ScheduledFire
  trigger + the head-at-fire ``predecessor_seq`` the bridge computes);
* ids are derived deterministically from the door's idempotency key, so a replay/double
  submit converges on the same rows (the A4-D-X / A10-D-6 convergence convention).

Extracted verbatim from ``origination_service`` (a behaviour-identical move — the A4
origination suites are the regression proof, per A10-D-8's gate).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from persona.schedules import Schedule
from persona.tasks import Task, TaskState, WaitKind

from persona_api.tasks.scheduled_fire import TASK_SCHEDULED_FIRE_JOB_TYPE

if TYPE_CHECKING:
    from datetime import datetime

    from persona.schedules import RecurrenceRule
    from persona.tasks import Contract

__all__ = [
    "build_backing_schedule",
    "build_backing_task",
    "derive_task_and_schedule_ids",
]


def _derive_id(prefix: str, key: str) -> str:
    """A deterministic id from an idempotency key (stable across replays)."""
    return f"{prefix}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]}"


def derive_task_and_schedule_ids(key: str) -> tuple[str, str]:
    """The deterministic ``(task_id, schedule_id)`` pair for one idempotency key.

    One key ⇒ one pair, always: a re-delivered event (A4) or a double-submitted form
    (A10) re-derives the same ids and converges on the existing rows.
    """
    return _derive_id("task", key), _derive_id("sched", key)


def build_backing_task(
    *,
    task_id: str,
    owner_id: str,
    persona_id: str,
    contract: Contract,
    conversation_id: str | None,
    schedule_id: str | None,
    now: datetime,
) -> Task:
    """Construct the A2 task carrying the contract (the matrix rides ``contract``).

    A schedule-backed task is born **WAITING(until_time)** — dormant at zero cost, awaiting
    its first scheduled fire, which the leg handler resumes (the existing WAITING→ACTIVE
    resume). A scheduleless task stays DEFINED (the pre-schedule shape).
    ``conversation_id`` is ``None`` for a calendar-created task (no originating chat turn).
    """
    scheduled = schedule_id is not None
    return Task(
        id=task_id,
        owner_id=owner_id,
        persona_id=persona_id,
        contract=contract,
        conversation_id=conversation_id,
        schedule_id=schedule_id,
        state=TaskState.WAITING if scheduled else TaskState.DEFINED,
        wait_kind=WaitKind.UNTIL_TIME if scheduled else None,
        created_at=now,
        updated_at=now,
    )


def build_backing_schedule(
    *,
    schedule_id: str,
    owner_id: str,
    timezone: str,
    recurrence: RecurrenceRule | None,
    one_time_at: datetime | None,
    task_id: str,
    now: datetime,
) -> Schedule:
    """Construct the A1 schedule that fires the task's legs (the A1→A2 fire bridge).

    Targets the schedule→leg bridge, NOT ``task_leg`` directly: the leg needs a
    ScheduledFire trigger + the head-at-fire ``predecessor_seq`` the bridge computes
    (a static template can't carry it).
    """
    return Schedule(
        id=schedule_id,
        owner_id=owner_id,
        timezone=timezone,
        recurrence=recurrence,
        one_time_at=one_time_at,
        target_job_type=TASK_SCHEDULED_FIRE_JOB_TYPE,
        payload_template={"task_id": task_id},
        created_at=now,
        updated_at=now,
    )
