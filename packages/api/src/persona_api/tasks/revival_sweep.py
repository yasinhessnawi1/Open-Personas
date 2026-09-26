"""The revival sweep — work that stopped for a reason that has passed (Spec W1, T8).

Two ways a task ends up alive but with nothing running, and one sweep that answers both.

**Stranded (R9-148).** Several places CONSUME a task's leg without running it: the claim-side
guard skips a paused task's job, the kill-switch guard skips a job while owner autonomy is
paused or the persona is suspended, and a control that stops a leg mid-flight enqueues no
continuation. The job succeeds, the task stays ``active``, and nothing is left to move it. A
scheduled task is rescued by its next fire; an ad hoc one waits forever. Clearing the reason
(resuming autonomy, unsuspending the persona) does not by itself put the work back, and
D-W1-30's resume seam only covers the door the user pressed. So: an ``active``, unpaused,
runnable task with no live job and nothing pending, idle past a grace window, is re-enqueued
from its head. One sweep covers autonomy resume, persona unsuspend and any future
consume-at-claim, which is why no per-control revival code exists.

**A transient failure (D-W1-8).** A leg that dead-lettered because the world was briefly
unavailable — a rate limit, a provider wobble, an empty completion — is parked
``waiting(on_user)`` by the dead-leg sweep with an honest offer. For those causes ONLY, and at
most ONCE per park, and only after :data:`~persona.tasks.TRANSIENT_RETRY_AFTER`, the sweep
picks it up on the user's behalf. A deterministic cause (over budget, a tool that is not
allowed, a broken contract) is never picked up: trying again spends the owner's credits on
work that cannot succeed, so it stays an offer the user can accept.

Both halves refuse to act when the owner's autonomy is paused, when the persona is suspended,
or when the task is superseded by a newer one with the same goal (D-W1-9) — the user asked for
it again themselves, and reviving the old one would do the work twice.

Leader-gated on its OWN advisory lock, distinct from the scheduler tick, the approval sweep and
the dead-leg sweep, so "one reviver" holds without contending any of them.
"""

from __future__ import annotations

import zlib
from datetime import timedelta
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.tasks import (
    TRANSIENT_RETRY_AFTER,
    AutoRetry,
    Revived,
    TaskState,
    WaitKind,
    classify_retryable,
)
from sqlalchemy import exists, select

from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import tasks as tasks_t
from persona_api.tasks.handler import TASK_LEG_JOB_TYPE
from persona_api.tasks.live_legs import live_task_leg_ids

if TYPE_CHECKING:
    from datetime import datetime

    from persona.tasks import Task
    from sqlalchemy import Engine

    from persona_api.approvals.kill_switch import KillSwitchStore
    from persona_api.schedules.leadership import SchedulerLeader
    from persona_api.tasks.continuation import TaskContinuation
    from persona_api.tasks.store import TaskStore

__all__ = [
    "REVIVAL_AUDIT_ACTION",
    "REVIVAL_SWEEP_LOCK_KEY",
    "STRANDED_GRACE",
    "RevivalSweeper",
    "normalise_goal",
]

_log = get_logger("api.tasks.revival_sweep")

#: A stable 64-bit key for the revival-sweep leader lock, distinct from every other sweep's.
REVIVAL_SWEEP_LOCK_KEY: int = zlib.crc32(b"persona:revival:sweep:leader")

#: How long a task must have sat untouched before the sweep calls it stranded. A leg that just
#: finished has a moment between its checkpoint landing and its continuation being enqueued;
#: reviving inside that window would run the same leg twice. Well past any such gap.
STRANDED_GRACE = timedelta(minutes=10)

#: The audit action a revival writes. It is also how ONCE is enforced: the sweep looks for its
#: own row at this head before acting, so a park is picked up at most once no matter how many
#: ticks see it (D-W1-8).
REVIVAL_AUDIT_ACTION = "task.revived"

#: The one-line note the revival records, so the audit trail says who moved the task and why.
_STRANDED_NOTE = "stranded: no live leg"
#: What the revived leg is TOLD, in the reconstruction it reads (D-W1-38). The audit note above
#: is for the operator; this is for the persona, so it says the thing in the persona's terms.
_STRANDED_REASON = "nothing was running it, so it was put back where it stopped"
_TRANSIENT_NOTE = "transient failure retried"


def normalise_goal(goal: str) -> str:
    """The comparable form of a goal (Spec W1, D-W1-9): casefolded, whitespace collapsed.

    Deliberately not a model call and not fuzzy. Supersession decides whether to skip work the
    user has evidently asked for again, and a cheap exact-after-normalising match is honest
    about what it can catch: the same request typed again. Anything looser risks skipping a
    revival because two different jobs read alike.
    """
    return " ".join(goal.split()).casefold()


class RevivalSweeper:
    """Puts back work that stopped for a reason that has since passed.

    Args:
        continuation: The A2 continuation owning the resume seam (owner-scoped, and the one
            that keys past spent attempts, D-W1-29).
        dispatch_engine: The worker's cross-tenant engine — the candidate scan reads every
            tenant's rows, exactly like the dead-letter read the dead-leg sweep does.
        rls_engine: The owner-scoped engine every ACT goes through.
        task_store: The RLS-scoped task store.
        kill_switch: The pause authority. Both halves refuse while any pause holds.
        leader: The leadership gate on :data:`REVIVAL_SWEEP_LOCK_KEY`.
        limit: Max candidates per half per tick (bounded work).
    """

    def __init__(
        self,
        *,
        continuation: TaskContinuation,
        dispatch_engine: Engine,
        rls_engine: Engine,
        task_store: TaskStore,
        kill_switch: KillSwitchStore,
        leader: SchedulerLeader,
        limit: int = 50,
    ) -> None:
        self._continuation = continuation
        self._dispatch = dispatch_engine
        self._engine = rls_engine
        self._tasks = task_store
        self._kill_switch = kill_switch
        self._leader = leader
        self._limit = limit

    async def run_once(self, *, now: datetime) -> int:
        """Leader-gated: revive what can be revived. Returns how many tasks were put back."""
        if not self._leader.try_become_leader():
            return 0  # a follower does no work — at most one reviver runs
        revived = self._revive_stranded(now=now) + self._revive_transient_parks(now=now)
        if revived:
            _log.info("revival sweep put tasks back", revived=revived)
        return revived

    # --- stranded: alive, runnable, and nothing left to run it (R9-148) ----------------------

    def _revive_stranded(self, *, now: datetime) -> int:
        """Re-enqueue an ``active`` task whose leg was consumed without running."""
        cutoff = now - STRANDED_GRACE
        stmt = (
            select(tasks_t.c.id, tasks_t.c.owner_id)
            .where(
                tasks_t.c.state == TaskState.ACTIVE.value,
                tasks_t.c.paused.is_(False),
                tasks_t.c.updated_at < cutoff,
                ~exists(live_task_leg_ids(tasks_t.c.id)),
            )
            .order_by(tasks_t.c.updated_at)
            .limit(self._limit)
        )
        revived = 0
        with self._dispatch.begin() as conn:
            candidates = [(str(r.id), str(r.owner_id)) for r in conn.execute(stmt)]
        for task_id, owner_id in candidates:
            if self._revive(owner_id, task_id, now=now, note=_STRANDED_NOTE, trigger=None):
                revived += 1
        return revived

    # --- a transient failure, offered and then taken up (D-W1-8) -----------------------------

    def _revive_transient_parks(self, *, now: datetime) -> int:
        """Pick up a task the dead-leg sweep parked, when its cause was the world, not the work."""
        eligible_since = now - TRANSIENT_RETRY_AFTER
        stmt = (
            select(tasks_t.c.id, tasks_t.c.owner_id)
            .where(
                tasks_t.c.state == TaskState.WAITING.value,
                tasks_t.c.wait_kind == WaitKind.ON_USER.value,
                tasks_t.c.paused.is_(False),
                tasks_t.c.updated_at < eligible_since,
            )
            .order_by(tasks_t.c.updated_at)
            .limit(self._limit)
        )
        with self._dispatch.begin() as conn:
            candidates = [(str(r.id), str(r.owner_id)) for r in conn.execute(stmt)]
        revived = 0
        for task_id, owner_id in candidates:
            cause = self._dead_cause_at_head(task_id, self._head_of(task_id))
            if cause is None or not classify_retryable(cause):
                continue  # offered, never taken up on the user's behalf
            if self._revive(
                owner_id,
                task_id,
                now=now,
                note=_TRANSIENT_NOTE,
                trigger=AutoRetry(cause=cause, retried_at=now),
            ):
                revived += 1
        return revived

    def _head_of(self, task_id: str) -> int | None:
        """This task's head checkpoint, read cross-tenant with the candidate scan."""
        with self._dispatch.begin() as conn:
            return conn.execute(
                select(tasks_t.c.head_checkpoint_seq).where(tasks_t.c.id == task_id)
            ).scalar()

    def _dead_cause_at_head(self, task_id: str, head: int | None) -> str | None:
        """The cause of the dead leg AT THIS HEAD, if there is one (cross-tenant read).

        Matching on the task alone was wrong, and wrong in a way that acted: a task that
        dead-lettered transiently at head H, was picked up by the user, ran, and then parked
        for a DIFFERENT reason still carries that dead row, so the sweep would read it as a
        fresh transient failure and resume a leg the task is not waiting for. The head is what
        makes a dead row current, so the key is what is matched: ``task:{id}:after:{head}``,
        with or without the ``:retry:N`` suffix a later attempt appends.

        That makes ``head_checkpoint_seq`` a correlation key as well as a chain pointer
        (R9-173). The key is minted by ``persona_api.tasks.handler.task_leg_idempotency_key``
        and the field is defined on ``persona.tasks.entity.Task``; the three sites must agree.
        The rule that follows: a park or gate path that appends a checkpoint must re-key or
        re-enqueue, or the dead-job match is lost. The stuck park records no obstacle
        checkpoint for exactly this reason (``TaskContinuation.react_to_dead_leg``), and the
        approval gate may append because no dead job exists at its head.
        """
        prefix = f"task:{task_id}:after:{_head_key(head)}"
        stmt = (
            select(jobs_t.c.last_error)
            .where(
                jobs_t.c.type == TASK_LEG_JOB_TYPE,
                jobs_t.c.state == "dead",
                (jobs_t.c.idempotency_key == prefix)
                | jobs_t.c.idempotency_key.like(f"{prefix}:retry:%"),
            )
            .order_by(jobs_t.c.created_at.desc())
            .limit(1)
        )
        with self._dispatch.begin() as conn:
            row = conn.execute(stmt).first()
        return str(row.last_error) if row is not None and row.last_error else None

    # --- the one act, and everything that can refuse it --------------------------------------

    def _revive(
        self,
        owner_id: str,
        task_id: str,
        *,
        now: datetime,
        note: str,
        trigger: AutoRetry | None,
    ) -> bool:
        """Put ONE task back, if every gate agrees. Owner-scoped throughout.

        The sweep loop carries no ambient tenant context, so the owner's scope is bound here
        for the RLS reads and writes, exactly as the dead-leg sweep does, and reset after.
        """
        from persona_api.middleware.rls_context import current_user_id

        token = current_user_id.set(owner_id)
        try:
            task = self._tasks.get(owner_id, task_id)
            # Re-read under the owner's scope: the candidate scan is a snapshot, and a user may
            # have paused, cancelled or answered in between.
            if not self._kill_switch.is_runnable(owner_id, task):
                return False
            if self._already_revived(owner_id, task_id, task.head_checkpoint_seq):
                return False  # at most once per park (D-W1-8)
            if self._awaiting_an_approval(owner_id, task_id):
                # The task waits on a DECISION, not on the world. Resuming here would run a
                # leg the user has not authorised, straight past the refusal the reply route
                # enforces. The same exclusion the attention list makes.
                _log.info("revival skipped: an approval is pending", task_id=task_id)
                return False
            if self._superseded(owner_id, task):
                _log.info("revival skipped: superseded by a newer task", task_id=task_id)
                return False
            self._continuation.resume(
                owner_id,
                task_id,
                trigger
                if trigger is not None
                # Spec W1 (D-W1-38): nothing failed and no schedule fired, so the next leg is
                # told what actually happened. A ScheduledFire here would have it believe its
                # own schedule woke it.
                else Revived(reason=_STRANDED_REASON, revived_at=now),
                now=now,
            )
            # AFTER the enqueue, never before. A marker written first would survive a resume
            # that raised, and the task could then never be revived at this head again: the
            # sweep would have stranded the very task it exists to rescue, while the audit row
            # claimed it was saved. Writing it second risks only a repeat attempt, and a repeat
            # cannot double-run, because the second resume computes the SAME idempotency key (a
            # queued job is not a spent attempt) and A0's duplicate guard absorbs it.
            self._record(owner_id, task, note=note)
            _log.info("task revived", task_id=task_id, why=note)
        except Exception as exc:  # noqa: BLE001 — one bad task must never stop the sweep
            _log.warning("revival failed task_id={tid}: {err}", tid=task_id, err=str(exc))
            return False
        else:
            return True
        finally:
            current_user_id.reset(token)

    def _awaiting_an_approval(self, owner_id: str, task_id: str) -> bool:
        """Is this task parked on a pending proposal? Then it waits on a decision, not on us."""
        from persona_api.approvals.store import ApprovalStore

        return ApprovalStore(self._engine).get_pending_for_task(owner_id, task_id) is not None

    def _already_revived(self, owner_id: str, task_id: str, head: int | None) -> bool:
        """Has this sweep already put this task back at THIS head? (The once rule.)"""
        stmt = select(
            exists(
                select(audit_log_t.c.id).where(
                    audit_log_t.c.user_id == owner_id,
                    audit_log_t.c.action == REVIVAL_AUDIT_ACTION,
                    audit_log_t.c.target == task_id,
                    audit_log_t.c.metadata["head"].as_string() == _head_key(head),
                )
            )
        )
        with self._engine.begin() as conn:
            return bool(conn.execute(stmt).scalar())

    def _superseded(self, owner_id: str, task: Task) -> bool:
        """Is there a NEWER live task for the same persona with the same goal? (D-W1-9.)

        The user asking for the same thing again is the clearest possible signal that the old
        attempt is not wanted; reviving it would do the work twice and bill for both.
        """
        goal = normalise_goal(task.contract.goal)
        for other in self._tasks.list_for_owner(owner_id):
            if other.id == task.id or other.persona_id != task.persona_id:
                continue
            if other.created_at <= task.created_at:
                continue
            if other.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED):
                continue
            if normalise_goal(other.contract.goal) == goal:
                return True
        return False

    def _record(self, owner_id: str, task: Task, *, note: str) -> None:
        """Write the once-marker BEFORE the resume, so a crash cannot revive twice."""
        from persona_api.services import audit_service

        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action=REVIVAL_AUDIT_ACTION,
            target=task.id,
            metadata={"head": _head_key(task.head_checkpoint_seq), "why": note},
        )


def _head_key(head: int | None) -> str:
    """The head a revival is keyed on; ``init`` before the first checkpoint (the leg-key shape).

    Must stay byte-identical to the anchor ``task_leg_idempotency_key`` writes (R9-173): the
    sweep matches dead jobs on it, so the two spellings drifting apart reads as "no dead leg".
    """
    return "init" if head is None else str(head)
