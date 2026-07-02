"""The A1→A2 handoff: a schedule fire becomes a task leg (Spec A4 schedule-attach).

A1's scheduler tick fires a schedule as a generic job whose payload is the handoff *anchor*
(``schedule_id`` + ``fire_time``) merged with the schedule's ``payload_template`` (the ``task_id``).
A2's leg handler, though, needs a :class:`TaskLegPayload` — a discriminated ``ScheduledFire``
trigger plus the **head-at-fire-time** ``predecessor_seq`` (the A2-R-4 anchor), which a static
schedule
template cannot carry (the head advances every occurrence). This thin bridge closes that gap:

    schedule fire → :class:`ScheduledTaskFireHandler` reads the task's current head → enqueues a
    proper ``task_leg`` (predecessor_seq fixed = head) → the leg runs (A2-R-4 protects re-delivery).

Keeping the fire → leg step an *enqueue* (not a direct run) preserves the store-CAS idempotency of
the leg: a re-delivered ``task_leg`` re-keys to the same seq and no-ops. Without this handoff an
origination-created schedule fires into a payload the leg handler can't parse — the exact reason A4
tasks never executed (they were inert).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access (payload field)
from typing import TYPE_CHECKING

from persona.jobs import LONG_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.tasks import ScheduledFire

from persona_api.tasks.handler import enqueue_task_leg

if TYPE_CHECKING:
    from persona.jobs import JobContext, JobRegistry

    from persona_api.jobs.queue import JobQueue
    from persona_api.tasks.store import TaskStore

__all__ = [
    "TASK_SCHEDULED_FIRE_JOB_TYPE",
    "ScheduledTaskFireHandler",
    "TaskScheduledFirePayload",
    "register_scheduled_task_fire_handler",
]

TASK_SCHEDULED_FIRE_JOB_TYPE = "task_scheduled_fire"

_log = get_logger("api.tasks.scheduled_fire")


class TaskScheduledFirePayload(JobPayload):
    """A schedule fire targeting a task — the A1 anchor + the task id (the schedule's template)."""

    task_id: str
    schedule_id: str
    fire_time: datetime


class ScheduledTaskFireHandler:
    """Turn one schedule fire into a task leg at the task's current head (A2-R-4-safe)."""

    def __init__(self, *, task_store: TaskStore, queue: JobQueue) -> None:
        self._tasks = task_store
        self._queue = queue

    async def handle(self, payload: TaskScheduledFirePayload, context: JobContext) -> None:
        """Read the task head + enqueue the leg carrying a ``ScheduledFire`` trigger.

        A terminal/cancelled task is a benign no-op (a fire arriving after the task ended — e.g. a
        one-time schedule's fire racing a cancel); the leg handler's own guard also skips it.
        """
        task = self._tasks.get(context.owner_id, payload.task_id)
        enqueue_task_leg(
            self._queue,
            owner_id=context.owner_id,
            task_id=payload.task_id,
            predecessor_seq=task.head_checkpoint_seq,  # fixed head = the A2-R-4 anchor for this leg
            trigger=ScheduledFire(schedule_id=payload.schedule_id, fire_time=payload.fire_time),
        )
        _log.info(
            "scheduled fire → task leg enqueued",
            task_id=payload.task_id,
            schedule_id=payload.schedule_id,
        )


def register_scheduled_task_fire_handler(
    registry: JobRegistry, *, task_store: TaskStore, queue: JobQueue
) -> None:
    """Register the ``task_scheduled_fire`` tenant (the schedule→leg bridge)."""
    registry.register(
        JobTypeSpec(
            type=TASK_SCHEDULED_FIRE_JOB_TYPE,
            payload_model=TaskScheduledFirePayload,
            handler=ScheduledTaskFireHandler(task_store=task_store, queue=queue),
            idempotency_key=lambda p: f"schedfire:{p.task_id}:{p.fire_time.isoformat()}",
            retry=RetryPolicy(max_attempts=3),
            lease=LONG_LEASE,
        )
    )
