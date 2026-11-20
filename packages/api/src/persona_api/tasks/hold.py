"""Hold an answer that arrives when its task cannot run (R9-240, R9-239 part 1).

Two doors answer a task that waits on the user: a reply (``POST /tasks/{id}/reply``) and an
approval decision (the inbox, or chat). Both queued the next leg whatever the task's state,
and two states make that leg worthless or worse:

- **Paused.** The claim consumes a paused task's leg without running it. A task that parked
  on an approval and was paused afterwards then stayed WAITING, and Resume on a waiting task
  only clears the pause, so nothing ever ran again.
- **At its budget cap.** The leg is boxed at zero remaining budget: it stops at once, pauses
  again, and the answer rode a leg that did nothing with it.

The answer is not lost (D-ORCH-11-18-d (2) and (3)). It is folded into the head first: an
approval's own resolution checkpoint, or a ``reply`` checkpoint for a reply. Then the task is
HELD: moved WAITING to ACTIVE, and at the cap given the budget pause. ACTIVE and paused is
exactly the shape Resume and Raise cap already carry on from (each queues a leg from the
head), so the answer reaches the next leg through its checkpoint and neither control needs a
second way to restart work.

**The hold decides again after it moves the task** (PR 34 review). The reason it was called
with is a read taken before its own transition: a Resume or a Raise cap can land in between,
and each of those queues nothing for a task that is still WAITING. So once the task is ACTIVE
the hold reads it again, and a task that nothing holds any more is carried on (its leg is
queued) rather than left ACTIVE, unpaused and with nothing to run.

A reply to a task the USER paused is still refused (D-W1-10); only an approval decision is
carried out under a pause, because it decides on an action already pending rather than
adding input for the next leg.

Not one transaction: the answer is durable before the task moves. A failure after that is
answered truthfully by the caller (the answer is kept; the user is told to pick the task up),
and a retry of the same reply on the same head writes nothing new.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from persona.errors import TaskStateError
from persona.logging import get_logger
from persona.tasks import (
    CheckpointKind,
    Revived,
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
    "APPROVAL_HELD_AT_THE_CAP",
    "APPROVAL_HELD_WHILE_PAUSED",
    "APPROVAL_HOLD_FAILED",
    "REPLY_RACED_NOTE",
    "REPLY_SAVED_AT_THE_CAP_NOTE",
    "REPLY_SAVED_BUT_NOT_HELD_NOTE",
    "REPLY_TOO_LONG_AT_THE_CAP_NOTE",
    "HoldOutcome",
    "HoldReason",
    "SavedReply",
    "TaskHold",
    "held_approval_note",
]

_log = get_logger("api.tasks.hold")

#: What the reply door answers when it saved the reply at the cap (D-ORCH-11-18-d (3)).
REPLY_SAVED_AT_THE_CAP_NOTE = (
    "Your reply is saved. This task has used its budget, so it carries on with your answer "
    "when you raise the cap with Raise cap on the task page."
)
#: A reply too long to keep as a checkpoint until the task carries on (D-ORCH-11-18-e (2)).
#: Nothing is changed, so the answer is still in the user's reply box.
REPLY_TOO_LONG_AT_THE_CAP_NOTE = (
    "This task has used its budget, and this answer is too long to keep until it carries "
    "on. Raise the cap with Raise cap on the task page, then answer."
)
#: Another write moved the task's head while this reply was being saved (a double submit
#: of a different answer). Nothing of this reply was kept.
REPLY_RACED_NOTE = (
    "This task changed while your reply was being saved, so it was not saved. Try again."
)
#: The reply is durable, but holding the task for Raise cap failed after it.
REPLY_SAVED_BUT_NOT_HELD_NOTE = (
    "Your reply is saved, but the task could not be set to carry on by itself. Raise the cap "
    "with Raise cap on the task page, then pick the task up."
)

#: The resolver's machine notes for an answered approval whose next leg waits. Like the
#: resolver's other notes (``reconfirm``, ``race_lost``), they are codes the web renders in
#: its own copy, on the inbox card and in chat.
APPROVAL_HELD_WHILE_PAUSED = "held_paused"
APPROVAL_HELD_AT_THE_CAP = "held_at_cap"
#: The decision was carried out, but holding the task failed after it.
APPROVAL_HOLD_FAILED = "hold_failed"

#: What the carried-on leg is told woke it (D-W1-38): not a schedule, not a Resume press.
_CARRIED_ON_REASON = "the user answered, and nothing holds the task back any more"


class HoldReason(StrEnum):
    """Why the next leg waits. Paused is checked first: Resume then refuses at the cap and
    points to Raise cap, so a task that is both still lands where the user can move it."""

    PAUSED = "paused"
    BUDGET = "budget"


class HoldOutcome(StrEnum):
    """Where a hold left the task, read after its own transition."""

    #: ACTIVE under a pause: Resume or Raise cap carries it on.
    HELD = "held"
    #: Nothing held it by then (a Resume or Raise cap landed first): its leg is queued.
    CARRIED_ON = "carried_on"
    #: It finished in between (a cancel): there is nothing left to carry on.
    FINISHED = "finished"


class SavedReply(StrEnum):
    """What saving a reply did."""

    SAVED = "saved"
    #: The same reply on the same head was saved before (a retry): nothing new written.
    ALREADY_SAVED = "already_saved"
    #: Another write took the head first, and it is not this reply: nothing was kept.
    LOST_RACE = "lost_race"


def held_approval_note(reason: HoldReason) -> str:
    """The resolver's note for an approval whose next leg waits for ``reason``."""
    if reason is HoldReason.PAUSED:
        return APPROVAL_HELD_WHILE_PAUSED
    return APPROVAL_HELD_AT_THE_CAP


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

    def save_reply(self, owner_id: str, task: Task, reply: str, *, now: datetime) -> SavedReply:
        """Fold ``reply`` into a new head checkpoint, answering whatever was open.

        The next leg is woken by Raise cap, not by the reply, so it is told through the
        checkpoint: the answer joins the conclusions in words that name the question, and
        the question leaves ``open_questions`` (a leg resumed by a reply starts from nothing
        open, D-W1-35; this record does the same). Everything else rides forward unchanged,
        as the approval resolution's record does (D-W1-16).

        Idempotent on the head: the same reply already saved there is not saved twice. The
        append is a compare-and-set, so a head another write took first is reported, never
        mistaken for this reply being saved.

        Raises:
            CheckpointTooLargeError: The record would exceed the checkpoint token budget;
                nothing was written.
        """
        checkpoints = CheckpointStore(self._engine)
        prior = checkpoints.get_latest(owner_id, task.id)
        if _is_this_reply(task.id, prior, reply):
            return SavedReply.ALREADY_SAVED
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
        _, applied = checkpoints.append_applied(task, record, spend={}, now=now)
        if applied:
            return SavedReply.SAVED
        if _is_this_reply(task.id, checkpoints.get_latest(owner_id, task.id), reply):
            return SavedReply.ALREADY_SAVED
        return SavedReply.LOST_RACE

    def hold(
        self, owner_id: str, task_id: str, reason: HoldReason, *, now: datetime
    ) -> HoldOutcome:
        """Move the task to ACTIVE under its pause, then decide again on what it holds now.

        WAITING moves to ACTIVE (the answer it waited for has arrived); the pause overlay is
        untouched by that move. At the cap the budget gate then takes the budget pause, under
        its own row lock, with its ``budget.reached`` record; if the gate fails, a plain pause
        stands in, so the task is still where Raise cap carries it on. Call after the answer
        is folded into the head: on the community engine a write from another connection
        waits on the gate's lock.
        """
        task = self._tasks.get(owner_id, task_id)
        if task.state is TaskState.WAITING:
            try:
                task = self._tasks.resume(owner_id, task_id, now=now)
            except TaskStateError:
                # Lost a race (the review's N2): a cancel landed after the read above.
                if is_terminal(self._tasks.get(owner_id, task_id).state):
                    return HoldOutcome.FINISHED
                raise
        if reason is HoldReason.BUDGET:
            self._pause_at_the_cap(owner_id, task, now=now)
        current = self._tasks.get(owner_id, task_id)
        if is_terminal(current.state):
            return HoldOutcome.FINISHED
        if current.paused:
            _log.info("answer held; next leg waits", task_id=task_id, reason=reason.value)
            return HoldOutcome.HELD
        # A Resume or a Raise cap landed between the caller's read and this move, and queued
        # nothing because the task was still WAITING then. Carry it on from the answer.
        from persona_api.jobs.queue import JobQueue
        from persona_api.tasks.continuation import TaskContinuation

        TaskContinuation(
            task_store=self._tasks,
            queue=JobQueue(self._engine),
            checkpoint_store=CheckpointStore(self._engine),
        ).resume(owner_id, task_id, Revived(reason=_CARRIED_ON_REASON, revived_at=now), now=now)
        _log.info("answer arrived and nothing held the task; carried on", task_id=task_id)
        return HoldOutcome.CARRIED_ON

    def _pause_at_the_cap(self, owner_id: str, task: Task, *, now: datetime) -> None:
        """The budget pause, or a plain pause if the gate itself fails (never left unpaused)."""
        try:
            self._budget().enforce(owner_id, task, now=now)
        except Exception as exc:  # noqa: BLE001 - fall back to the pause Raise cap still lifts
            _log.warning(
                "budget pause failed; pausing plainly task_id={tid}: {err}",
                tid=task.id,
                err=str(exc),
            )
            current = self._tasks.get(owner_id, task.id)
            if not current.paused and not is_terminal(current.state):
                try:
                    self._tasks.pause(owner_id, task.id, now=now)
                except TaskStateError:
                    # A Pause or a cancel landed first; the hold's own re-read then decides.
                    now_is = self._tasks.get(owner_id, task.id)
                    if not (now_is.paused or is_terminal(now_is.state)):
                        raise

    def _at_the_cap(self, owner_id: str, task: Task) -> bool:
        from persona_api.approvals.budget import BudgetState

        return self._budget().check(owner_id, task) is BudgetState.REACHED

    def _budget(self) -> BudgetEnforcer:
        from persona_api.approvals.budget import BudgetEnforcer
        from persona_api.jobs.queue import JobQueue

        return BudgetEnforcer(engine=self._engine, tasks=self._tasks, queue=JobQueue(self._engine))


def _is_this_reply(task_id: str, head: TaskCheckpoint | None, reply: str) -> bool:
    """Is ``head`` the reply record that saved exactly ``reply``?"""
    if head is None or checkpoint_kind(task_id, head.leg_id) is not CheckpointKind.REPLY:
        return False
    return bool(head.progress_conclusions) and head.progress_conclusions[-1].endswith(f": {reply}")
