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

**The deleted-executor degrade (Spec A10, A10-D-7):** a schedule outlives a persona it names.
Probed (not assumed): deleting a persona CASCADE-deletes its tasks (``tasks.persona_id``
``ON DELETE CASCADE``) while the schedule row SURVIVES (schedules carry no persona FK) — so the
schedule kept re-arming and every fire failed ``task not found`` into a silent retry →
dead-letter loop, forever, the user never told. The CASCADE also means task-present-but-
persona-absent is unreachable, so the orphan signal IS the missing task: on
``TaskNotFoundError`` at the bridge, the schedule is **paused** (the existing audited door) and
a **durable P6 notification** tells the user why — never a system-voiced fire (the persona-ness
guarantee: a persona's fire sounds like that persona or does not fire), never a tick/worker
crash, never a silent retry storm. This also retro-fixes the pre-existing A4-era orphan
(persona deletion always left its schedules firing into the void).
"""

from __future__ import annotations

from datetime import UTC, datetime  # noqa: TC003 — Pydantic needs runtime access (payload field)
from typing import TYPE_CHECKING

from persona.errors import TaskNotFoundError
from persona.jobs import LONG_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.tasks import ScheduledFire

from persona_api.db.engine import rls_connection
from persona_api.services.notifications_service import create_notification
from persona_api.tasks.handler import enqueue_task_leg

if TYPE_CHECKING:
    from persona.jobs import JobContext, JobRegistry
    from sqlalchemy import Engine

    from persona_api.jobs.queue import JobQueue
    from persona_api.schedules.store import ScheduleStore
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

    def __init__(
        self,
        *,
        task_store: TaskStore,
        queue: JobQueue,
        schedule_store: ScheduleStore,
        rls_engine: Engine,
    ) -> None:
        self._tasks = task_store
        self._queue = queue
        self._schedules = schedule_store
        self._engine = rls_engine

    async def handle(self, payload: TaskScheduledFirePayload, context: JobContext) -> None:
        """Read the task head + enqueue the leg carrying a ``ScheduledFire`` trigger.

        A terminal/cancelled task is a benign no-op (a fire arriving after the task ended — e.g. a
        one-time schedule's fire racing a cancel); the leg handler's own guard also skips it.
        A CASCADE-orphaned schedule (its task gone with a deleted executor persona) degrades to
        pause + notification (A10-D-7), never a leg, never a retry loop.
        """
        try:
            task = self._tasks.get(context.owner_id, payload.task_id)
        except TaskNotFoundError:
            self._degrade_missing_executor(
                owner_id=context.owner_id,
                task_id=payload.task_id,
                schedule_id=payload.schedule_id,
            )
            return
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

    def _degrade_missing_executor(self, *, owner_id: str, task_id: str, schedule_id: str) -> None:
        """Pause the schedule + tell the user durably (A10-D-7) — no leg, no crash, no retry.

        The missing task is the deleted-executor signal (the persona CASCADE took the task; the
        schedule survived). Pause rides the existing CAS-guarded, audited door
        (``schedule.pause``); the P6 bell entry is idempotent on ``(owner, kind, ref_id)`` so a
        re-delivered fire job cannot duplicate it. The job completes normally — an orphaned
        schedule is a handled outcome, not a retryable error (retrying cannot resurrect a
        deleted persona).
        """
        self._schedules.pause(owner_id, schedule_id, now=datetime.now(UTC))
        with rls_connection(self._engine, owner_id) as conn:
            create_notification(
                conn=conn,
                owner_id=owner_id,
                kind="schedule_executor_missing",
                ref_id=schedule_id,
                level="warning",
                message_key="notifications.schedule.executor_missing",
                params={"task_id": task_id, "schedule_id": schedule_id},
            )
        _log.warning(
            "scheduled fire's task is gone (executor persona deleted) — schedule paused + "
            "user notified",
            task_id=task_id,
            schedule_id=schedule_id,
        )


def register_scheduled_task_fire_handler(
    registry: JobRegistry,
    *,
    task_store: TaskStore,
    queue: JobQueue,
    schedule_store: ScheduleStore,
    rls_engine: Engine,
) -> None:
    """Register the ``task_scheduled_fire`` tenant (the schedule→leg bridge)."""
    registry.register(
        JobTypeSpec(
            type=TASK_SCHEDULED_FIRE_JOB_TYPE,
            payload_model=TaskScheduledFirePayload,
            handler=ScheduledTaskFireHandler(
                task_store=task_store,
                queue=queue,
                schedule_store=schedule_store,
                rls_engine=rls_engine,
            ),
            idempotency_key=lambda p: f"schedfire:{p.task_id}:{p.fire_time.isoformat()}",
            retry=RetryPolicy(max_attempts=3),
            lease=LONG_LEASE,
        )
    )
