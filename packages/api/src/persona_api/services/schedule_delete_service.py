"""Delete a schedule with durable user-intent recording (Spec R9, R9-037).

The route-facing orchestration for ``DELETE /v1/me/schedule/{schedule_id}``: records
the durable tombstone (:mod:`persona_api.schedules.tombstones`) every autonomous
origination seam must now consult, AND resolves the task-bridge semantic — a
schedule-backed task whose schedule the user just deleted must not sit silently
WAITING forever, looking alive while it can never fire again.

Composes two existing, unchanged stores (``ScheduleStore.delete`` keeps its own
``schedule.delete`` audit row exactly as before; ``TaskStore.pause`` is the SAME
overlay A3's budget/dead-leg sweeps already use) — this module adds no new
mutation primitive, only the R9-037 orchestration around them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.errors import TaskStateError
from persona.logging import get_logger
from persona.schedules import render_human_terms

from persona_api.db.engine import rls_connection
from persona_api.schedules.tombstones import TombstoneAction, extract_subject, normalize_title
from persona_api.services import notifications_service

if TYPE_CHECKING:
    from persona.schedules import Schedule
    from persona.tasks import Task
    from sqlalchemy import Engine

    from persona_api.realtime.channel import UserEventChannel
    from persona_api.schedules.store import ScheduleStore
    from persona_api.schedules.tombstones import ScheduleTombstoneStore
    from persona_api.tasks.store import TaskStore

__all__ = ["ScheduleDeleteOutcome", "delete_schedule_with_intent"]

_log = get_logger("api.schedule_delete")


@dataclass(frozen=True)
class ScheduleDeleteOutcome:
    """What :func:`delete_schedule_with_intent` did (route-facing confirmation)."""

    schedule_id: str
    paused_task_id: str | None


def delete_schedule_with_intent(
    *,
    schedule_store: ScheduleStore,
    task_store: TaskStore,
    tombstones: ScheduleTombstoneStore,
    rls_engine: Engine,
    owner_id: str,
    schedule_id: str,
    event_channel: UserEventChannel | None = None,
    now: datetime | None = None,
) -> ScheduleDeleteOutcome:
    """Delete a schedule, record the tombstone, and pause an orphaned linked task.

    Raises :class:`~persona.errors.ScheduleNotFoundError` (unchanged — 404 upstream)
    if the schedule is absent; ``ScheduleStore.delete``'s RLS scoping/audit shape is
    untouched.

    Order — each step past the delete is best-effort (the user's requested delete
    always completes even if the ancillary honesty work below it degrades):

    1. read the schedule (captures the essence for the tombstone) + find a linked
       task BEFORE deleting — ``tasks.schedule_id`` is ``ON DELETE SET NULL``, so the
       linkage is unrecoverable once the delete commits;
    2. delete via the existing ``ScheduleStore.delete`` (its own ``schedule.delete``
       audit row fires exactly as before — untouched);
    3. record the tombstone (``ScheduleTombstoneStore.record``) — the durable "the
       user deleted this" fact every origination seam now consults (design point 1);
    4. the task-bridge semantic (design point 3): if a task was linked and is still
       non-terminal/unpaused, PAUSE it (the standing ``TaskStore.pause`` overlay A3's
       budget/dead-leg sweeps already use) — deleting the schedule stops the task's
       recurrence; the task must not silently re-arm (nothing re-creates its schedule
       — R9-037's own gates see to that), but it is also not silently forgotten, so a
       P6 bell notification tells the user why (mirrors A10-D-7's
       ``schedule_executor_missing`` shape — the reverse-direction sibling of this
       exact "one half of a pair outlived the other" problem).
    """
    now = now or datetime.now(UTC)
    schedule = schedule_store.get(owner_id, schedule_id)  # ScheduleNotFoundError -> 404 upstream
    linked_task = task_store.get_by_schedule_id(owner_id, schedule_id)

    schedule_store.delete(owner_id, schedule_id)

    subject = extract_subject(schedule.payload_template)
    title_key = normalize_title(subject) if subject is not None else None
    reason: dict[str, str] = {
        "cadence": render_human_terms(
            recurrence=schedule.recurrence,
            one_time_at=schedule.one_time_at,
            timezone=schedule.timezone,
        )
    }
    if subject is not None:
        reason["subject"] = subject
    tombstones.record(
        owner_id,
        schedule_id=schedule_id,
        persona_id=_resolve_persona_id(schedule, linked_task),
        target_job_type=schedule.target_job_type,
        title_key=title_key,
        action=TombstoneAction.DELETED,
        reason=reason,
        now=now,
    )

    paused_task_id: str | None = None
    if linked_task is not None:
        paused_task_id = _pause_orphaned_task(
            task_store=task_store,
            rls_engine=rls_engine,
            owner_id=owner_id,
            task=linked_task,
            schedule_id=schedule_id,
            event_channel=event_channel,
            now=now,
        )
    return ScheduleDeleteOutcome(schedule_id=schedule_id, paused_task_id=paused_task_id)


def _resolve_persona_id(schedule: Schedule, linked_task: Task | None) -> str | None:
    """The R9-024 two-linkage-kind resolution (task-join, else the payload)."""
    if linked_task is not None:
        return linked_task.persona_id
    raw = schedule.payload_template.get("persona_id")
    return raw if isinstance(raw, str) else None


def _pause_orphaned_task(
    *,
    task_store: TaskStore,
    rls_engine: Engine,
    owner_id: str,
    task: Task,
    schedule_id: str,
    event_channel: UserEventChannel | None,
    now: datetime,
) -> str | None:
    """Pause the task; ``None`` (benign no-op) if it is already terminal/paused."""
    try:
        task_store.pause(owner_id, task.id, now=now)
    except TaskStateError:
        _log.info(
            "schedule delete: linked task not pausable (terminal or already paused) "
            "task_id={tid} schedule_id={sid}",
            tid=task.id,
            sid=schedule_id,
        )
        return None
    _log.info(
        "schedule delete: linked task paused — its schedule is gone (R9-037) "
        "task_id={tid} schedule_id={sid}",
        tid=task.id,
        sid=schedule_id,
    )
    with rls_connection(rls_engine, owner_id) as conn:
        notifications_service.create_notification(
            conn=conn,
            owner_id=owner_id,
            kind="schedule_deleted_task_paused",
            ref_id=task.id,
            level="info",
            message_key="notifications.schedule.taskPaused",
            params={"task_id": task.id},
        )
    notifications_service.publish_notification_created(
        event_channel,
        owner_id=owner_id,
        kind="schedule_deleted_task_paused",
        ref_id=task.id,
    )
    return task.id
