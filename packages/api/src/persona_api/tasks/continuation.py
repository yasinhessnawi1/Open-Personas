"""Task continuation — disposition → state machine + the resume seam (Spec A2, T8).

Turns a leg's :class:`~persona_runtime.legs.LegOutcome` into the task's next move, and owns
the resume path. The three continuations:

- **CONTINUE (immediate)** — more work now: the task stays ``active`` and the next leg is
  enqueued immediately (rides A0; the A2-R-4 key dedups a double-enqueue).
- **CONTINUE + ``resume_at`` → ``waiting(until_time)``** — a "re-check in 4h" leg: the task goes
  ``waiting(until_time)`` and the next leg is enqueued with ``scheduled_at = resume_at`` (rides
  A0's ``scheduled_at``, the same mechanism A1's tick uses). Dormant: a queued-not-claimed job
  + a state row — no leg running, no box, no held connection.
- **COMPLETED** — the leg reached ``[FINAL]``: the task completes (the completion report is T9).

A leg **FAILED** raises :class:`~persona.errors.TaskLegFailedError` so A0 re-delivers (transient;
the leg appended nothing, so the head is unadvanced and the retry re-runs it). Exhaustion is
A0's dead-letter; the task→FAILED reaction is A3/T9.

**``waiting(on_user)``** is A3/A4-driven (an approval / a question — the leg poses something):
:meth:`wait_on_user` parks the task at **zero cost** (a state row, NO job). The reply/event
arrives later → :meth:`resume` enqueues the next leg carrying the trigger into its
reconstruction. ``EventTrigger`` stays the reserved seam (no producer in v1, D-A2-5); the
reply-injection is the defined ``UserReply`` path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from persona.errors import ScheduleNotFoundError, TaskLegFailedError
from persona.logging import get_logger
from persona.schedules import next_fire_after
from persona.tasks import (
    ScheduledFire,
    TaskState,
    WaitKind,
    build_cancellation_summary,
    build_stuck_report,
)
from persona_runtime.legs import LegDisposition

from persona_api.tasks.handler import TASK_LEG_JOB_TYPE, enqueue_task_leg

if TYPE_CHECKING:
    from datetime import datetime

    from persona.tasks import (
        CancellationSummary,
        ResumeTrigger,
        StuckReport,
        Task,
        TaskCheckpoint,
    )
    from persona_runtime.legs import LegOutcome

    from persona_api.jobs.queue import JobQueue
    from persona_api.schedules.store import ScheduleStore
    from persona_api.tasks.store import CheckpointStore, TaskStore

#: The A11/A6 task.updated signal seam (owner_id, task_id, state) → best-effort live ping. The
#: worker binds it to ``publish_task_updated`` over the user event channel; routes/tests leave it
#: ``None`` (the surface catches up on its next poll/navigation — the durable floor).
TaskStateSignal = Callable[[str, str, str], None]

__all__ = ["TaskContinuation", "TaskStateSignal"]

_log = get_logger("api.tasks.continuation")


class TaskContinuation:
    """Applies a leg outcome to the task lifecycle + owns the resume / failure / cancel paths."""

    def __init__(
        self,
        *,
        task_store: TaskStore,
        queue: JobQueue,
        checkpoint_store: CheckpointStore | None = None,
        schedule_store: ScheduleStore | None = None,
        on_state_change: TaskStateSignal | None = None,
    ) -> None:
        self._tasks = task_store
        self._queue = queue
        self._checkpoints = checkpoint_store
        # Spec A4 recurrence: a leg completing on a RECURRING schedule-backed task is one
        # OCCURRENCE done, not the task — so the task returns to WAITING(until_time) for the next
        # scheduled fire (the schedule drives it) instead of terminating. Without a schedule store
        # a completed leg always terminates (the pre-recurrence A2 shape).
        self._schedules = schedule_store
        # Spec A11/A6 (W8): the task.updated live-refetch ping, fired on the transitions A6's
        # surfaces care about — terminal + waiting(on_user). Best-effort + AFTER the durable write
        # (surface-lags-truth). None → no ping (the surface catches up on its next poll).
        self._on_state_change = on_state_change

    def _signal(self, owner_id: str, task_id: str, state: str) -> None:
        """Best-effort task.updated ping — a subscriber failure never breaks the transition."""
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(owner_id, task_id, state)
        except Exception:  # noqa: BLE001 — a live-ping failure must not fail the state write
            _log.warning("task.updated signal failed; degrading to poll", task_id=task_id)

    def apply(
        self,
        owner_id: str,
        outcome: LegOutcome,
        *,
        now: datetime,
        fired_at: datetime | None = None,
        event_fired: bool = False,
    ) -> None:
        """Drive the task's next move from a leg outcome.

        ``outcome.task`` is the post-append task (head advanced) for CONTINUE/COMPLETED.
        ``fired_at`` is the scheduled instant that triggered this leg (a :class:`ScheduledFire`'s
        ``fire_time``), used to decide recurrence — "is there a fire strictly AFTER this one?" —
        independent of when the leg executes; defaults to ``now`` for non-scheduled continuations.
        ``event_fired`` (Spec A7, A7-D-X-event-standing) marks a leg the A7 dispatcher fired via an
        :class:`~persona.tasks.EventFire`: a COMPLETED such leg returns to WAITING(on_event)
        — a standing "whenever X" watch keeps reacting (the echo said "whenever"; a one-fire death
        would break echo-honesty). It carries no schedule, so ``_recurs`` is moot.

        Raises:
            TaskLegFailedError: On a FAILED leg (so A0 re-delivers — transient).
        """
        task = outcome.task
        if outcome.disposition == LegDisposition.COMPLETED:
            if event_fired:
                # A7 standing watch: the event-fired occurrence is done → back to WAITING(on_event),
                # ready for the next matching event (the dispatcher's door-a fires the next leg).
                self._tasks.begin_wait(owner_id, task.id, WaitKind.ON_EVENT, now=now)
                _log.info("event occurrence complete → waiting(on_event)", task_id=task.id)
            elif self._recurs(owner_id, task, after=fired_at if fired_at is not None else now):
                # One occurrence done, but the schedule has a future fire — return to
                # WAITING(until_time); the next scheduled fire resumes it (no enqueue here, the
                # schedule drives the next leg). This is what makes a recurring task RECUR.
                self._tasks.begin_wait(owner_id, task.id, WaitKind.UNTIL_TIME, now=now)
                _log.info("recurring occurrence complete → waiting(until_time)", task_id=task.id)
            else:
                self._tasks.complete(owner_id, task.id, now=now)
                _log.info("task completed", task_id=task.id)
                self._signal(owner_id, task.id, TaskState.COMPLETED.value)  # terminal — A11 ping
            return
        if outcome.disposition == LegDisposition.WAITING_APPROVAL:
            # A3 gate: the leg recorded a durable proposal and ended (no append). Park the task
            # waiting(on_user) at zero cost — the user resolves it in the Approvals inbox or by
            # replying in chat (both wired via ApprovalResolutionService). NOTE: the proactive C0
            # "may I do X?" voice on park (ApprovalResolver.announce) is NOT yet wired — a known
            # notify-on-park gap; discovery is inbox/chat-driven until it is.
            self.wait_on_user(owner_id, task.id, now=now)
            _log.info(
                "task waiting(on_user) — approval",
                task_id=task.id,
                proposal_id=outcome.proposal_id,
            )
            return
        if outcome.disposition == LegDisposition.FAILED:
            raise TaskLegFailedError("task leg failed; retry", context={"task_id": task.id})
        # CONTINUE — another leg follows this checkpoint.
        predecessor = task.head_checkpoint_seq
        if outcome.resume_at is not None:
            self._tasks.begin_wait(owner_id, task.id, WaitKind.UNTIL_TIME, now=now)
            self._enqueue_next(owner_id, task.id, predecessor, outcome.resume_at)
            _log.info("task waiting(until_time)", task_id=task.id)
        else:
            self._enqueue_next(owner_id, task.id, predecessor, now)  # immediate continuation

    def _recurs(self, owner_id: str, task: Task, *, after: datetime) -> bool:
        """True iff the task's schedule has a fire strictly AFTER the one that just fired.

        Recomputes ``next_fire_after`` from the schedule's rule + anchor (race-free — independent of
        the mutable ``next_fire_at`` column the tick advances in a separate txn) relative to
        ``after`` = this occurrence's fire instant. A recurring rule with a next occurrence →
        survive; a
        one-time schedule or an exhausted COUNT/UNTIL rule → ``None`` → terminate. No schedule store
        wired, or a scheduleless task → not recurring (terminate — the pre-recurrence shape).
        """
        if self._schedules is None or task.schedule_id is None:
            return False
        try:
            schedule = self._schedules.get(owner_id, task.schedule_id)
        except ScheduleNotFoundError:
            return False  # the schedule is gone (deleted/compensated) → nothing to recur on
        return next_fire_after(schedule, after=after) is not None

    def wait_on_user(self, owner_id: str, task_id: str, *, now: datetime) -> None:
        """Park the task on the user at ZERO cost (a state row, no job). A3/A4 drive this.

        The leg posed an approval/question (C0 delivers it); the task is dormant until
        :meth:`resume` is called with the reply. No job is enqueued — that is the zero-cost.
        """
        self._tasks.begin_wait(owner_id, task_id, WaitKind.ON_USER, now=now)
        _log.info("task waiting(on_user)", task_id=task_id)
        self._signal(owner_id, task_id, TaskState.WAITING.value)  # waiting_on_user — A11 ping

    def resume(
        self,
        owner_id: str,
        task_id: str,
        trigger: ResumeTrigger,
        *,
        now: datetime,  # noqa: ARG002 — kept for call-site symmetry; transition is at pickup
    ) -> None:
        """The resume seam (TaskResumer) — a reply/fire/event arrived; enqueue the next leg.

        Enqueues a leg carrying ``trigger`` into its reconstruction (the reply lands in the
        next leg's trigger context). The handler performs the ``waiting → active`` transition
        when it picks the job up (one resume point). ``now`` is accepted for symmetry; the
        durable transition happens at pickup.
        """
        task = self._tasks.get(owner_id, task_id)
        enqueue_task_leg(
            self._queue,
            owner_id=owner_id,
            task_id=task_id,
            predecessor_seq=task.head_checkpoint_seq,
            trigger=trigger,
        )
        _log.info("task resume enqueued", task_id=task_id, trigger=trigger.kind)

    # --- failure (A0 dead-letter → waiting(on_user)) + cancellation ---------

    def react_to_dead_leg(
        self, owner_id: str, task_id: str, cause: str, *, now: datetime
    ) -> StuckReport | None:
        """React to A0's REAL dead-letter (jobs.state='dead'): park the task on the user.

        Failure-after-retries is NOT a silent terminal — the task transitions
        ``active → waiting(on_user)`` with an honest :class:`StuckReport` (the dead job's
        ``last_error`` as the real cause + where it stood). A3 voices it; the user can resume
        or cancel; A3's reminder/auto-pause keeps it from haunting (no zombie). **Idempotent**:
        a dead job processed twice (or for an already-parked/terminal task) is a no-op — the
        ``active``-only guard is the single check (no second exhaustion derivation).

        Returns the :class:`StuckReport`, or ``None`` if the task was not active (no-op).
        """
        task = self._tasks.get(owner_id, task_id)
        if task.state != TaskState.ACTIVE:
            return None  # already reacted / waiting / terminal — idempotent
        checkpoint = self._latest_checkpoint(owner_id, task_id)
        report = build_stuck_report(task, checkpoint, cause=cause, now=now)
        self._tasks.begin_wait(owner_id, task_id, WaitKind.ON_USER, now=now)
        _log.info("task stuck → waiting(on_user)", task_id=task_id, cause=cause)
        self._signal(owner_id, task_id, TaskState.WAITING.value)  # stuck→waiting_on_user — A11 ping
        return report

    def sweep_dead_legs(
        self, dead_letter_queue: JobQueue, *, now: datetime, limit: int = 50
    ) -> int:
        """Read A0's dead-letter queue and react to each dead ``task_leg`` job.

        The cross-tenant read of ``dead_letters()`` is the worker's (a privileged queue); the
        per-task reaction is owner-scoped. Returns the number of tasks parked.
        """
        reacted = 0
        for job in dead_letter_queue.dead_letters(limit=limit):
            if job.type != TASK_LEG_JOB_TYPE:
                continue
            task_id = str(job.payload.get("task_id", ""))
            if not task_id:
                continue
            cause = job.last_error or "leg failed after retries"
            if self.react_to_dead_leg(job.owner_id, task_id, cause, now=now) is not None:
                reacted += 1
        return reacted

    def cancel(self, owner_id: str, task_id: str, *, now: datetime) -> CancellationSummary:
        """User-initiated cancel → a clean terminal state + an honest where-things-stood.

        The latest checkpoint is the durable where-it-stood (finalised at the prior leg end);
        the task lands ``cancelled``. A leg in flight finishes its box and its append no-ops
        against the now-terminal task (cancel wins; no corruption) — the cooperative mid-leg
        ``CancelToken`` trip is the executor's ``external_cancel`` seam (T6), wired by the
        worker's cancel signal at deploy.
        """
        task = self._tasks.get(owner_id, task_id)
        summary = build_cancellation_summary(
            task, self._latest_checkpoint(owner_id, task_id), now=now
        )
        self._tasks.cancel(owner_id, task_id, now=now)
        _log.info("task cancelled", task_id=task_id)
        self._signal(owner_id, task_id, TaskState.CANCELLED.value)  # terminal — A11 ping
        return summary

    def _latest_checkpoint(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:
        return (
            self._checkpoints.get_latest(owner_id, task_id)
            if self._checkpoints is not None
            else None
        )

    def _enqueue_next(
        self, owner_id: str, task_id: str, predecessor: int | None, fire_time: datetime
    ) -> None:
        """Enqueue the next leg (immediate if ``fire_time`` is now; scheduled if future)."""
        trigger = ScheduledFire(schedule_id=f"self:{task_id}", fire_time=fire_time)
        enqueue_task_leg(
            self._queue,
            owner_id=owner_id,
            task_id=task_id,
            predecessor_seq=predecessor,
            trigger=trigger,
            scheduled_at=fire_time,
        )
