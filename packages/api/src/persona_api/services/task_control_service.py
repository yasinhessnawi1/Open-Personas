"""The task controls behind every door (Spec W1, T2; D-W1-1 / D-W1-14).

Under "a run is an execution of a task" there is one set of controls, and they act on the
task whichever surface the user came through: the task detail, the review page, or the run
viewer of one of its runs. This module is where cancel lives so the run viewer's cancel and
the task route's cancel are the same code, not two doors that drift.

The schedule mirror (R9-108) goes through :class:`ScheduleStore`'s own API and nothing
else: A10-D-9 permits exactly one schedule write path, and the guard test pins it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.tasks import Revived, TaskState, WaitKind

from persona_api.approvals.budget import BudgetEnforcer, BudgetState
from persona_api.approvals.kill_switch import KillSwitchStore
from persona_api.jobs.queue import JobQueue
from persona_api.schedules import ScheduleStore
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from persona.tasks import CancellationSummary, Task
    from sqlalchemy import Engine

__all__ = [
    "PICKUP_AUDIT_ACTION",
    "PICKUP_REPLY",
    "ControlOutcome",
    "TaskControlMutator",
    "cancel_task",
    "mirror_schedule_pause",
    "pause_task",
    "pickup_task",
    "reply_to_task",
    "resume_task",
    "retry_task",
]

_log = get_logger("api.services.task_control")


def _kill_switch(engine: Engine) -> KillSwitchStore:
    return KillSwitchStore(
        engine,
        continuation=TaskContinuation(
            task_store=TaskStore(engine),
            queue=JobQueue(engine),
            checkpoint_store=CheckpointStore(engine),
            schedule_store=ScheduleStore(engine),
        ),
    )


def mirror_schedule_pause(engine: Engine, owner_id: str, task: Task, *, paused: bool) -> None:
    """Stop (or restart) the task's SCHEDULE alongside its ``paused`` overlay (R9-108).

    The leg handler already honours ``task.paused`` and skips the work, so pausing
    stopped anything running. But nothing stopped the schedule FIRING: production
    showed 47 fires against a paused task, each enqueueing a job that was
    immediately discarded, while the calendar still presented the task as live. The
    owner reasonably read that as "pause did nothing".

    Routed through :class:`ScheduleStore`'s own API rather than writing the table
    here: A10-D-9 permits exactly one schedule write path, and a second one is the
    very drift this is fixing. Best-effort — a task whose schedule is already gone,
    or a mirror that fails, must never block the task control the user pressed.
    """
    if task.schedule_id is None:
        return
    now = datetime.now(UTC)
    try:
        store = ScheduleStore(engine)
        if paused:
            store.pause(owner_id, task.schedule_id, now=now)
        else:
            store.resume(owner_id, task.schedule_id, now=now)
    except Exception as exc:  # noqa: BLE001 — the task control already succeeded
        _log.warning(
            "task {task_id}: schedule pause mirror failed ({error})",
            task_id=task.id,
            error=str(exc),
        )


def cancel_task(
    engine: Engine, owner_id: str, task: Task, *, now: datetime | None = None
) -> CancellationSummary:
    """Cancel ``task`` (terminal) and stop its schedule firing. One cancel for every door.

    The kill switch's cancel audits ``task.cancel`` and lands the terminal state; the
    schedule mirror then stops the cadence (R9-108's second half). A running leg finishes
    its current step and then stops (the external cancel seam is T6, D-W1-21).
    """
    at = now if now is not None else datetime.now(UTC)
    summary = _kill_switch(engine).cancel_task(owner_id, task.id, now=at)
    mirror_schedule_pause(engine, owner_id, task, paused=True)
    return summary


#: What a pickup says to the leg it resumes: the user asked for the work to continue.
PICKUP_REPLY = "Pick this up where you left off."

#: The audit action every pickup writes, whichever door it came through (Spec W1, T9).
PICKUP_AUDIT_ACTION = "task.pickup"

#: What Resume answers on a task that has used its budget (R9-158; owner ruling 2026-09-26).
#: Resuming it would run a leg boxed at zero remaining budget, which stops at once and pauses
#: again at the cap. Extending is the way on, and it resumes the task by itself. "Raise cap" is
#: the task page's label for Extend; the chat door reaches the same sentence.
BUDGET_REACHED_RESUME_NOTE = (
    "This task has used its budget, so resuming it would stop again at once. "
    "Extend the budget with Raise cap on the task page and it carries on by itself."
)

#: The same refusal for a task waiting on the user (R9-158). Raising its cap does not queue
#: a leg (one would run past what it is waiting on), so "carries on by itself" would be false:
#: the user raises the cap, then answers it or picks it up.
BUDGET_REACHED_WAITING_NOTE = (
    "This task has used its budget. Extend the budget with Raise cap on the task page, "
    "then answer it or pick it up and it carries on."
)

#: What a resumed leg is told (D-W1-38): a person lifted the pause, nothing failed, nothing
#: was answered. The persona reads this, so it says the thing in the persona's terms.
_RESUMED_REASON = "the user resumed it after a pause"


@dataclass(frozen=True)
class ControlOutcome:
    """The durable result of a pickup / reply / retry: what changed, and an honest note."""

    task: Task
    changed: bool
    note: str = ""
    owner_paused: bool = False
    successor: Task | None = None


def _continuation(engine: Engine, queue: JobQueue | None = None) -> TaskContinuation:
    return TaskContinuation(
        task_store=TaskStore(engine),
        queue=queue if queue is not None else JobQueue(engine),
        checkpoint_store=CheckpointStore(engine),
        schedule_store=ScheduleStore(engine),
    )


def pause_task(engine: Engine, owner_id: str, task: Task, *, now: datetime) -> ControlOutcome:
    """Pause: no new legs, a running leg stops at its next boundary (D-W1-21), and the schedule
    stops firing (R9-108). One pause for every door (route, chat verb)."""
    from persona.errors import TaskStateError
    from persona.tasks import is_terminal

    if task.paused:
        return ControlOutcome(task=task, changed=False, note="Already paused.")
    if is_terminal(task.state):
        return ControlOutcome(task=task, changed=False, note="This task has already finished.")
    try:
        paused = TaskStore(engine).pause(owner_id, task.id, now=now)
    except TaskStateError:
        return ControlOutcome(task=task, changed=False, note="This task has already finished.")
    mirror_schedule_pause(engine, owner_id, paused, paused=True)
    return ControlOutcome(task=paused, changed=True)


def resume_task(
    engine: Engine,
    owner_id: str,
    task: Task,
    *,
    now: datetime,
    queue: JobQueue | None = None,
) -> ControlOutcome:
    """Resume a paused task: clear the overlay AND put its next leg back on the worker.

    Spec W1 (R9-146, D-W1-30): a pause now reaches a running leg, which stops at its boundary
    and enqueues no continuation, and a leg claimed while paused is consumed without running.
    Either way a task that was being worked (``DEFINED`` / ``ACTIVE``) has no job left once it
    is paused, so clearing the overlay alone would leave it stalled forever; a scheduled task
    is rescued by its next fire, an ad hoc one never is. The resume therefore rides the
    continuation's resume seam from the salvaged head, whose key carries the spent-attempt
    suffix (D-W1-29), so the consumed row at that head cannot absorb it. A task waiting on the
    user or on the clock only needs the overlay cleared: its own trigger resumes it. The chat
    verb and the route share this one function.
    """
    from persona.tasks import is_terminal

    store = TaskStore(engine)
    # R9-158: checked first, so the pickup Resume becomes for a task waiting on the user is
    # refused with the same sentence (pickup_task carries the same guard for its own door).
    refused = _refused_at_the_cap(engine, owner_id, task)
    if refused is not None:
        return refused
    if not task.paused:
        # Spec W1 (T9): "start it again" means one thing to a user, and the seam should mean it
        # too. A task that is not paused but is parked ON THE USER is not resumed, it is picked
        # up — the same seam the review page's Pick up and the persona's own tool use. Without
        # this, "pick it back up" in chat (which the steering cue reads as RESUME) was answered
        # with "Not paused." and nothing happened.
        if task.state is TaskState.WAITING and task.wait_kind is WaitKind.ON_USER:
            return pickup_task(engine, owner_id, task, now=now)
        return ControlOutcome(task=task, changed=False, note="Not paused.")
    if is_terminal(task.state):
        return ControlOutcome(task=task, changed=False, note="This task has already finished.")
    unpaused = store.unpause(owner_id, task.id, now=now)  # audits task.unpause
    mirror_schedule_pause(engine, owner_id, unpaused, paused=False)
    owner_paused = KillSwitchStore(engine).is_owner_autonomy_paused(owner_id)
    note = (
        "Resumed, but your autonomy is paused, so it won't run until you resume autonomy."
        if owner_paused
        else ""
    )
    if unpaused.state is TaskState.WAITING:
        return ControlOutcome(task=unpaused, changed=True, note=note, owner_paused=owner_paused)
    if unpaused.state is TaskState.DEFINED:
        unpaused = store.start(owner_id, task.id, now=now)
    _continuation(engine, queue).resume(
        owner_id,
        task.id,
        # Spec W1 (D-W1-38, amended): the leg is told what actually woke it. A self
        # ScheduledFire had it believe its own schedule fired, when a person pressed Resume.
        Revived(reason=_RESUMED_REASON, revived_at=now),
        now=now,
    )
    _log.info("task resumed; next leg enqueued from the head", task_id=task.id)
    return ControlOutcome(task=unpaused, changed=True, note=note, owner_paused=owner_paused)


def _refused_at_the_cap(engine: Engine, owner_id: str, task: Task) -> ControlOutcome | None:
    """Refuse a control that would run a leg on a task that has used its budget (R9-158).

    The leg would be boxed at zero remaining budget: it stops at once and pauses again at
    the cap, recreating the stale "paused task, cancelled run" pair and spending to do it.
    Read from the budget state (spent at or over the effective cap), not from which door
    paused the task, so Resume and Pick up (the user's, and the persona's own tool) refuse
    alike, with one sentence that points to Extend. ``None`` lets the control go on.
    """
    from persona.tasks import is_terminal

    if is_terminal(task.state):
        return None
    budget = BudgetEnforcer(engine=engine, tasks=TaskStore(engine), queue=JobQueue(engine))
    if budget.check(owner_id, task) is not BudgetState.REACHED:
        return None
    waiting_on_user = task.state is TaskState.WAITING and task.wait_kind is WaitKind.ON_USER
    note = BUDGET_REACHED_WAITING_NOTE if waiting_on_user else BUDGET_REACHED_RESUME_NOTE
    return ControlOutcome(task=task, changed=False, note=note)


class TaskControlMutator:
    """The steering verbs' task mutator, routed through the SAME controls the routes use.

    Spec W1 (R9-146): the chat verb's resume used to clear the overlay only, and its pause
    never mirrored the schedule; both drifted from the route (the R9-081 shape). ``get`` reads
    the store; ``pause`` / ``unpause`` / ``cancel`` are the control-service functions.
    """

    def __init__(self, engine: Engine, *, queue: JobQueue | None = None) -> None:
        self._engine = engine
        self._queue = queue

    def get(self, owner_id: str, task_id: str) -> Task:
        return TaskStore(self._engine).get(owner_id, task_id)

    def pause(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        return pause_task(self._engine, owner_id, self.get(owner_id, task_id), now=now).task

    def unpause(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        return resume_task(
            self._engine, owner_id, self.get(owner_id, task_id), now=now, queue=self._queue
        ).task

    def cancel(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        task = self.get(owner_id, task_id)
        cancel_task(self._engine, owner_id, task, now=now)
        return self.get(owner_id, task_id)


def _blocked(engine: Engine, owner_id: str, task: Task) -> tuple[bool, str, bool]:
    """Whether a control that would run a leg is held, and the sentence that says why.

    D-W1-10 as ruled: a paused task, a paused owner or a suspended persona cannot pick up.
    The pickup is refused with the reason; nothing is enqueued that the claim would only skip.
    """
    switch = KillSwitchStore(engine)
    if task.paused:
        return True, "This task is paused. Resume it first.", False
    if switch.is_owner_autonomy_paused(owner_id):
        return True, "Your autonomy is paused, so nothing will run until you resume it.", True
    if switch.is_persona_suspended(owner_id, task.persona_id):
        return True, "This persona is suspended, so it cannot pick anything up.", False
    return False, "", False


def _audit_pickup(engine: Engine, owner_id: str, task_id: str, *, via: str) -> None:
    """Record who carried the work on (``user`` from a surface, ``persona`` from the tool)."""
    from persona_api.services import audit_service

    audit_service.record(
        engine=engine,
        user_id=owner_id,
        action=PICKUP_AUDIT_ACTION,
        target=task_id,
        metadata={"via": via},
    )


def pickup_task(
    engine: Engine, owner_id: str, task: Task, *, now: datetime, via: str = "user"
) -> ControlOutcome:
    """Pick up a task that waits on the user: enqueue its next leg (Spec W1, T6; D-W1-20).

    Rides the continuation's resume seam, so the resume key carries the retry suffix when the
    same head dead-lettered before (R9-130). A finished task is the durable truth, calmly.
    """
    from persona.tasks import UserReply, is_terminal

    if is_terminal(task.state):
        return ControlOutcome(task=task, changed=False, note="This task has already finished.")
    refused = _refused_at_the_cap(engine, owner_id, task)
    if refused is not None:
        return refused
    if task.state is not TaskState.WAITING or task.wait_kind is not WaitKind.ON_USER:
        return ControlOutcome(task=task, changed=False, note="This task is already being worked.")
    held, why, owner_paused = _blocked(engine, owner_id, task)
    if held:
        return ControlOutcome(task=task, changed=False, note=why, owner_paused=owner_paused)
    _continuation(engine).resume(owner_id, task.id, UserReply(reply=PICKUP_REPLY), now=now)
    # Spec W1 (T9): every door that picks work up leaves the same row, with WHO in it. A
    # persona picking up its own stalled task is the case that makes this matter: the record
    # must say the machine did it, not the user.
    _audit_pickup(engine, owner_id, task.id, via=via)
    _log.info("task picked up", task_id=task.id, via=via)
    return ControlOutcome(task=task, changed=True)


def reply_to_task(
    engine: Engine, owner_id: str, task: Task, reply: str, *, now: datetime
) -> ControlOutcome:
    """Answer a task that waits on the user: the reply rides into its next leg (D-W1-4).

    Raises:
        ApprovalPendingError: The task's wait is a pending approval; that is answered as an
            approval (approve / decline), never as free text past the gate.
    """
    from persona.tasks import UserReply, is_terminal

    from persona_api.approvals.store import ApprovalStore
    from persona_api.errors import ApprovalPendingError

    if is_terminal(task.state):
        return ControlOutcome(task=task, changed=False, note="This task has already finished.")
    if task.state is not TaskState.WAITING or task.wait_kind is not WaitKind.ON_USER:
        return ControlOutcome(task=task, changed=False, note="This task is not waiting on you.")
    pending = ApprovalStore(engine).get_pending_for_task(owner_id, task.id)
    if pending is not None:
        raise ApprovalPendingError(
            "This one is waiting for a yes or a no. Answer the approval instead.",
            context={"task_id": task.id, "proposal_id": pending.proposal_id},
        )
    held, why, owner_paused = _blocked(engine, owner_id, task)
    if held:
        return ControlOutcome(task=task, changed=False, note=why, owner_paused=owner_paused)
    _continuation(engine).resume(owner_id, task.id, UserReply(reply=reply), now=now)
    _log.info("task replied to", task_id=task.id)
    return ControlOutcome(task=task, changed=True)


def retry_task(
    engine: Engine, owner_id: str, task: Task, *, now: datetime, queue: JobQueue | None = None
) -> ControlOutcome:
    """Run a finished task again as a NEW task with the same contract (Spec W1, T6).

    A FAILED (or cancelled) task is terminal by the state machine; the honest retry is a
    fresh task of the same kind and contract, dispatched through the ONE dispatch
    composition the one-off door uses (``work_dispatch_service.dispatch_task``), so it gets
    its own runs, checkpoints and budget, and a persona deleted since the failure is refused
    with a sentence before anything is written. The old task stays as the record.
    """
    import uuid

    from persona.tasks import Task as TaskEntity
    from persona.tasks import is_terminal

    from persona_api.services.work_dispatch_service import PERSONA_GONE_MESSAGE, dispatch_task

    if not is_terminal(task.state):
        return ControlOutcome(task=task, changed=False, note="This task is still going.")
    held, why, owner_paused = _blocked(engine, owner_id, task.model_copy(update={"paused": False}))
    if held:
        return ControlOutcome(task=task, changed=False, note=why, owner_paused=owner_paused)
    successor = TaskEntity(
        id=f"task_{uuid.uuid4().hex}",
        owner_id=owner_id,
        persona_id=task.persona_id,
        contract=task.contract,
        kind=task.kind,
        conversation_id=task.conversation_id,
        created_at=now,
        updated_at=now,
    )
    started = dispatch_task(
        engine,
        queue if queue is not None else JobQueue(engine),
        successor,
        now=now,
        persona_gone_message=PERSONA_GONE_MESSAGE,
    )
    _log.info("task retried as a new task", task_id=task.id, successor_id=successor.id)
    return ControlOutcome(task=task, changed=True, successor=started)
