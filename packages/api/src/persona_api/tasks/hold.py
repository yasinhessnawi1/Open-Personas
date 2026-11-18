"""Hold an answer that arrives when its task cannot run (R9-240, R9-239 part 1).

Two doors answer a task that waits on the user: a reply (``POST /tasks/{id}/reply``) and an
approval decision (the inbox, or chat). Both queued the next leg whatever the task's state,
and two states make that leg worthless or worse:

- **Paused.** The claim consumes a paused task's leg without running it. A task that parked
  on an approval and was paused afterwards then stayed WAITING, and Resume on a waiting task
  only clears the pause, so nothing ever ran again.
- **At its budget cap.** The leg is boxed at zero remaining budget: it stops at once, pauses
  again, and the answer rode a leg that did nothing with it.

The answer is never refused and never lost (D-ORCH-11-18-d (2) and (3)). It is folded into
the head first: an approval's own resolution checkpoint, or a ``reply`` checkpoint for a
reply. Then the task is HELD: moved WAITING to ACTIVE, and at the cap given the budget pause.
ACTIVE and paused is exactly the shape Resume and Raise cap already carry on from (each
queues a leg from the head), so the answer reaches the next leg through its checkpoint and
neither control needs a second way to restart work.

A reply to a task the USER paused is still refused (D-W1-10); only an approval decision is
carried out under a pause, because it decides on an action already pending rather than
adding input for the next leg.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.tasks import (
    CheckpointKind,
    TaskCheckpoint,
    TaskState,
    checkpoint_id,
    checkpoint_kind,
    is_terminal,
)

from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from datetime import datetime

    from persona.tasks import Task
    from sqlalchemy import Engine

    from persona_api.approvals.budget import BudgetEnforcer

__all__ = [
    "APPROVAL_HELD_AT_THE_CAP_NOTE",
    "APPROVAL_HELD_WHILE_PAUSED_NOTE",
    "REPLY_SAVED_AT_THE_CAP_NOTE",
    "HoldReason",
    "TaskHold",
    "held_approval_note",
]

_log = get_logger("api.tasks.hold")

#: What the reply door answers when it saved the reply at the cap (D-ORCH-11-18-d (3)).
REPLY_SAVED_AT_THE_CAP_NOTE = (
    "Your reply is saved. This task has used its budget, so it carries on with your answer "
    "when you raise the cap with Raise cap on the task page."
)
#: What an approval decision answers when it was carried out on a paused task (D-ORCH (2)).
APPROVAL_HELD_WHILE_PAUSED_NOTE = (
    "Done. This task is paused, so it carries on from here when you resume it."
)
#: What an approval decision answers when it was carried out at the cap.
APPROVAL_HELD_AT_THE_CAP_NOTE = (
    "Done. This task has used its budget, so it carries on from here when you raise the cap "
    "with Raise cap on the task page."
)


class HoldReason(StrEnum):
    """Why the next leg waits. Paused is checked first: Resume then refuses at the cap and
    points to Raise cap, so a task that is both still lands where the user can move it."""

    PAUSED = "paused"
    BUDGET = "budget"


def held_approval_note(reason: HoldReason) -> str:
    """What an approval decision tells the user when its next leg waits."""
    if reason is HoldReason.PAUSED:
        return APPROVAL_HELD_WHILE_PAUSED_NOTE
    return APPROVAL_HELD_AT_THE_CAP_NOTE


class TaskHold:
    """Decides whether an answer's next leg must wait, and holds the task when it must."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._tasks = TaskStore(engine)

    def reason(self, owner_id: str, task: Task) -> HoldReason | None:
        """Why ``task``'s next leg cannot run now, or ``None`` when it can."""
        if is_terminal(task.state):
            return None
        if task.paused:
            return HoldReason.PAUSED
        return HoldReason.BUDGET if self._at_the_cap(owner_id, task) else None

    def holds_a_saved_reply(self, owner_id: str, task: Task) -> bool:
        """Is this task already held at its cap on a reply saved earlier (a second reply)?"""
        if task.state is not TaskState.ACTIVE or not task.paused:
            return False
        head = CheckpointStore(self._engine).get_latest(owner_id, task.id)
        if head is None or checkpoint_kind(task.id, head.leg_id) is not CheckpointKind.REPLY:
            return False
        return self._at_the_cap(owner_id, task)

    def save_reply(self, owner_id: str, task: Task, reply: str, *, now: datetime) -> Task:
        """Fold ``reply`` into a new head checkpoint, answering whatever was open.

        The next leg is woken by Raise cap, not by the reply, so it is told through the
        checkpoint: the answer joins the conclusions in words that name the question, and
        the question leaves ``open_questions`` (a leg resumed by a reply starts from nothing
        open, D-W1-35; this record does the same). Everything else rides forward unchanged,
        as the approval resolution's record does (D-W1-16).
        """
        checkpoints = CheckpointStore(self._engine)
        prior = checkpoints.get_latest(owner_id, task.id)
        questions = prior.open_questions if prior is not None else ()
        answered = (
            f"The user answered ({'; '.join(questions)}): {reply}"
            if questions
            else f"The user replied: {reply}"
        )
        seq = task.next_checkpoint_seq
        record = TaskCheckpoint(
            task_id=task.id,
            leg_id=checkpoint_id(task.id, CheckpointKind.REPLY, seq),
            checkpoint_seq=seq,
            progress_conclusions=(
                *(prior.progress_conclusions if prior is not None else ()),
                answered,
            ),
            decisions=prior.decisions if prior is not None else (),
            lessons=prior.lessons if prior is not None else (),
            queries_run=prior.queries_run if prior is not None else (),
            sources_seen=prior.sources_seen if prior is not None else (),
            current_plan=prior.current_plan if prior is not None else (),
            next_step=prior.next_step if prior is not None else "",
            open_questions=(),
            artifact_pointers=prior.artifact_pointers if prior is not None else (),
            event_log_cursor=prior.event_log_cursor if prior is not None else None,
            blocked_on=None,
            updated_at=now,
        )
        return checkpoints.append(task, record, spend={}, now=now)

    def hold(self, owner_id: str, task_id: str, reason: HoldReason, *, now: datetime) -> Task:
        """Leave the task ACTIVE under a pause, so Resume or Raise cap carries it on.

        WAITING moves to ACTIVE (the answer it waited for has arrived); the pause overlay is
        untouched by that move. At the cap the budget gate then takes the budget pause, under
        its own row lock, with its ``budget.reached`` record. Call after the answer is folded
        into the head: on the community engine a write from another connection waits on the
        gate's lock.
        """
        task = self._tasks.get(owner_id, task_id)
        if task.state is TaskState.WAITING:
            task = self._tasks.resume(owner_id, task_id, now=now)
        if reason is HoldReason.BUDGET:
            self._budget().enforce(owner_id, task, now=now)
        _log.info("answer held; next leg waits", task_id=task_id, reason=reason.value)
        return self._tasks.get(owner_id, task_id)

    def _at_the_cap(self, owner_id: str, task: Task) -> bool:
        from persona_api.approvals.budget import BudgetState

        return self._budget().check(owner_id, task) is BudgetState.REACHED

    def _budget(self) -> BudgetEnforcer:
        from persona_api.approvals.budget import BudgetEnforcer
        from persona_api.jobs.queue import JobQueue

        return BudgetEnforcer(engine=self._engine, tasks=self._tasks, queue=JobQueue(self._engine))
