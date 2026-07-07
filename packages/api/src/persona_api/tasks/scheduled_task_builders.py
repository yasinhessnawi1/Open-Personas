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
    from pydantic import JsonValue

__all__ = [
    "build_backing_schedule",
    "build_backing_task",
    "derive_task_and_schedule_ids",
    "derive_trigger_id",
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


def derive_trigger_id(key: str) -> str:
    """The deterministic A7 event-trigger id for one idempotency key (replay-stable, Spec A7)."""
    return _derive_id("evttrigger", key)


def build_backing_task(
    *,
    task_id: str,
    owner_id: str,
    persona_id: str,
    contract: Contract,
    conversation_id: str | None,
    schedule_id: str | None,
    now: datetime,
    wait_on_event: bool = False,
) -> Task:
    """Construct the A2 task carrying the contract (the matrix rides ``contract``).

    A schedule-backed task is born **WAITING(until_time)** — dormant at zero cost, awaiting
    its first scheduled fire, which the leg handler resumes (the existing WAITING→ACTIVE
    resume). An **event-triggered** task (``wait_on_event``, Spec A7) is born **WAITING(on_event)**
    — dormant until the A7 dispatcher fires its leg (door-a); it carries no ``schedule_id``. A plain
    scheduleless task stays DEFINED (the pre-schedule shape). ``conversation_id`` is ``None`` for a
    calendar-created task (no originating chat turn).
    """
    scheduled = schedule_id is not None
    if wait_on_event:
        state, wait_kind = TaskState.WAITING, WaitKind.ON_EVENT
    elif scheduled:
        state, wait_kind = TaskState.WAITING, WaitKind.UNTIL_TIME
    else:
        state, wait_kind = TaskState.DEFINED, None
    return Task(
        id=task_id,
        owner_id=owner_id,
        persona_id=persona_id,
        contract=contract,
        conversation_id=conversation_id,
        schedule_id=schedule_id,
        state=state,
        wait_kind=wait_kind,
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
    notify_on_fire: bool = False,
    subject: str | None = None,
) -> Schedule:
    """Construct the A1 schedule that fires the task's legs (the A1→A2 fire bridge).

    Targets the schedule→leg bridge, NOT ``task_leg`` directly: the leg needs a
    ScheduledFire trigger + the head-at-fire ``predecessor_seq`` the bridge computes
    (a static template can't carry it).

    ``notify_on_fire`` opts the schedule into the coalesced fire bell (default False for
    A4/background schedules; the user reminder door sets it True). It is written to the
    ``notify_on_fire`` column (the authoritative store value + DEFAULT source) AND — only
    when True — snapshotted into ``payload_template`` (absence there means False, so a
    background schedule's template is byte-unchanged). The tick merges the template into
    each fire job, so the handler gates WITHOUT a schedule read (surfacing as
    ``TaskScheduledFirePayload.notify_on_fire``). ``subject`` (the reminder's goal line)
    rides the template the same way so the handler can title the bell entry; omitted when
    there is no user subject.
    """
    template: dict[str, JsonValue] = {"task_id": task_id}
    if notify_on_fire:
        template["notify_on_fire"] = True
    if subject is not None:
        template["subject"] = subject
    return Schedule(
        id=schedule_id,
        owner_id=owner_id,
        timezone=timezone,
        recurrence=recurrence,
        one_time_at=one_time_at,
        target_job_type=TASK_SCHEDULED_FIRE_JOB_TYPE,
        payload_template=template,
        notify_on_fire=notify_on_fire,
        created_at=now,
        updated_at=now,
    )
