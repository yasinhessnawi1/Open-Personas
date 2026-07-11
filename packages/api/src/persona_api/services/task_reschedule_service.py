"""Apply a user-confirmed conversational reschedule to a live task (Spec A8, T6).

The api side of the reschedule seam: the chat-turn worker hands this the ``task_rescheduled`` event
the runtime emitted AFTER the user confirmed the re-echoed new clause. It resolves the task's
``schedule_id``, builds the new schedule from the current row + the new cadence, and applies it
through the ONE CAS-guarded door (:func:`persona_api.schedules.reschedule.reschedule`,
``actor=user_via_chat``) — so the mid-flight-edit race guard + the old→new+actor audit all run.
The worker injects ``owner_id`` from its handle, so every mutation is RLS-scoped to the caller (a
cross-tenant reschedule is impossible — the task read + the door both scope to ``owner_id``).

``skip_next`` is the bounded special case (A8-D-2): suppress exactly the next occurrence, no cadence
change. A rescheduled task with no ``schedule_id`` (a non-schedule-backed task) is a logged no-op.
So is a reschedule that targets a one-time schedule which has already fired (R9-023) — the door's
model-level guard rejects the re-arm; there is no live HTTP response to surface it on from here (the
worker runs after the turn already streamed its reply), so it is a loud, specific log line instead.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from persona.errors import ScheduleNotFoundError, ScheduleStateError, TaskNotFoundError
from persona.logging import get_logger
from persona.schedules import RecurrenceRule

from persona_api.schedules.reschedule import RescheduleActor, reschedule

if TYPE_CHECKING:
    from collections.abc import Mapping

    from persona.schedules import Schedule
    from persona.tasks import Task
    from sqlalchemy import Engine

    from persona_api.schedules.store import ScheduleStore

__all__ = ["TaskScheduleReader", "TaskRescheduleService"]

_logger = get_logger("api.task_reschedule")


class TaskScheduleReader(Protocol):
    """The owner-scoped task read the reschedule needs (the real ``TaskStore`` satisfies this)."""

    def get(self, owner_id: str, task_id: str) -> Task: ...


class TaskRescheduleService:
    """Maps a ``task_rescheduled`` event to the owner-scoped schedule change through the door."""

    def __init__(
        self, *, task_reader: TaskScheduleReader, schedule_store: ScheduleStore, engine: Engine
    ) -> None:
        self._tasks = task_reader
        self._schedules = schedule_store
        self._engine = engine

    def reschedule(self, event_data: Mapping[str, object]) -> None:
        """Apply the confirmed reschedule; a missing task/schedule is a logged no-op."""
        owner_id = str(event_data["owner_id"])
        task_id = str(event_data["task_id"])
        now = datetime.now(UTC)
        try:
            task = self._tasks.get(owner_id, task_id)
        except TaskNotFoundError:
            _logger.warning("reschedule target task not found; skipping", task_id=task_id)
            return
        schedule_id = task.schedule_id
        if schedule_id is None:
            _logger.warning("rescheduled task has no schedule; skipping", task_id=task_id)
            return

        try:
            if bool(event_data.get("skip_next")):
                self._schedules.skip_next(owner_id, schedule_id, now=now)
                return
            self._apply_cadence(owner_id, schedule_id, event_data, now=now)
        except ScheduleNotFoundError:
            _logger.warning(
                "reschedule target schedule not found; skipping", schedule_id=schedule_id
            )
        except ScheduleStateError as exc:
            # R9-023: a fired one-time can never be re-armed — the model-level guard
            # (Schedule.with_next_fire) rejects it. The runtime already confirmed the
            # new time with the user in-conversation before this event fired, and this
            # runs on the worker AFTER the turn's response already streamed — there is
            # no synchronous channel back to tell the user it didn't apply. Logging with
            # a specific, searchable reason (rather than letting this fall into the
            # caller's generic catch-all) is the best this best-effort seam can do.
            _logger.warning(
                "reschedule target already fired; a one-time cannot be re-armed",
                schedule_id=schedule_id,
                error=str(exc),
            )

    def _apply_cadence(
        self, owner_id: str, schedule_id: str, event_data: Mapping[str, object], *, now: datetime
    ) -> None:
        current = self._schedules.get(owner_id, schedule_id)
        timezone = str(event_data.get("timezone") or current.timezone)
        rrule = event_data.get("recurrence_rrule")
        one_time_raw = event_data.get("one_time_at")
        recurrence = (
            RecurrenceRule.from_rrule_string(str(rrule)) if isinstance(rrule, str) else None
        )
        one_time = (
            datetime.fromisoformat(str(one_time_raw).replace("Z", "+00:00"))
            if isinstance(one_time_raw, str)
            else None
        )
        new_schedule: Schedule = current.model_copy(
            update={
                "recurrence": recurrence,
                "one_time_at": one_time,
                "timezone": timezone,
                "updated_at": now,
            }
        )
        reschedule(
            self._schedules,
            self._engine,
            owner_id=owner_id,
            schedule_id=schedule_id,
            new_schedule=new_schedule,
            actor=RescheduleActor.USER_VIA_CHAT,
            provenance="conversational reschedule (user confirmed)",
            now=now,
        )
