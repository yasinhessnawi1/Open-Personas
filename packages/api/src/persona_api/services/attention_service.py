"""One attention query: what needs the user, read once and shown everywhere (Spec W1, T5).

Two surfaces used to answer "does anything need me?" from two different reads. The review
page's ``waiting`` section listed pending approvals only, and its ``stuck`` section only
tasks in the FAILED state, so a task parked ``waiting(on_user)`` (the single most important
needs-attention case: the persona is blocked on the user) never appeared (R9-099). The nav
badge meanwhile counted the ACTIVE working set, so it read non-zero while the list was empty,
which a user reads as the product lying. D-W1-5 and D-W1-6 settle it: **attention** means
waiting on the user, dead-lettered offers, pending approvals and FAILED tasks inside the
digest's window; the badge equals the review list because this one function feeds both.

Pure composition over the owner's RLS engine (no runtime factory): every read is owner-scoped
through the stores it reuses. Read-only (CQS). Ordering is triage order: approvals oldest
first (nearest to expiry), then tasks waiting on the user oldest first, then FAILED newest
first (the digest's own order for its stuck section).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs the runtime type
from enum import StrEnum
from typing import TYPE_CHECKING

from persona.tasks import TaskState, WaitKind
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from persona_api.approvals.store import ApprovalStore
from persona_api.db.engine import rls_connection
from persona_api.db.models import jobs as jobs_t
from persona_api.tasks.handler import TASK_LEG_JOB_TYPE
from persona_api.tasks.reader import APITaskStateReader
from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

__all__ = [
    "TERMINAL_WINDOW",
    "AttentionItem",
    "AttentionKind",
    "AttentionReason",
    "count_attention",
    "list_attention",
]

#: How many most-recent terminal tasks the digest looks at; a FAILED task inside this window
#: is attention (D-W1-6 as ruled). The digest builder imports this so the two never drift.
TERMINAL_WINDOW = 25


class AttentionKind(StrEnum):
    """What kind of thing needs the user."""

    APPROVAL = "approval"
    WAITING_ON_USER = "waiting_on_user"
    FAILED = "failed"


class AttentionReason(StrEnum):
    """Why a task waits on the user, so the surface can offer the right action.

    ``APPROVAL`` never appears on a task item: a task whose wait is a pending proposal is
    listed ONCE, as the approval (which carries the task id), never twice.
    """

    APPROVAL = "approval"
    QUESTION = "question"
    STUCK = "stuck"
    WAITING = "waiting"


#: The verbs each shape offers; T6 makes them act, T7 renders them (D-W1-6: actions, not links).
_ACTIONS: dict[tuple[AttentionKind, AttentionReason], tuple[str, ...]] = {
    (AttentionKind.APPROVAL, AttentionReason.APPROVAL): ("approve", "decline"),
    (AttentionKind.WAITING_ON_USER, AttentionReason.QUESTION): ("reply", "cancel"),
    (AttentionKind.WAITING_ON_USER, AttentionReason.STUCK): ("pickup", "cancel"),
    (AttentionKind.WAITING_ON_USER, AttentionReason.WAITING): ("reply", "pickup", "cancel"),
    # A FAILED task is terminal: it cannot be picked up, only run again as a new task.
    (AttentionKind.FAILED, AttentionReason.STUCK): ("retry",),
}


class AttentionItem(BaseModel):
    """One thing that needs the user: what, why, whose, and what can be done about it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: AttentionKind
    reason: AttentionReason
    persona_id: str
    task_id: str | None
    proposal_id: str | None
    title: str
    detail: str = ""
    actions: tuple[str, ...] = ()
    since: datetime


def _dead_leg_causes(engine: Engine, owner_id: str, task_ids: Sequence[str]) -> dict[str, str]:
    """The real cause per task whose leg dead-lettered (``jobs.last_error``), one query.

    A task the dead-leg sweep parked carries no checkpoint of its own for the stop (the
    sweep voices a report, it does not append one), but the dead job row is durable and
    owner-scoped, so the cause is read from there. Newest dead attempt wins.
    """
    if not task_ids:
        return {}
    stmt = (
        select(
            jobs_t.c.payload["task_id"].as_string().label("task_id"),
            jobs_t.c.last_error,
        )
        .where(
            jobs_t.c.type == TASK_LEG_JOB_TYPE,
            jobs_t.c.state == "dead",
            jobs_t.c.payload["task_id"].as_string().in_(list(task_ids)),
        )
        .order_by(jobs_t.c.created_at.asc())
    )
    causes: dict[str, str] = {}
    with rls_connection(engine, owner_id) as conn:
        for row in conn.execute(stmt):
            if row.last_error:
                causes[str(row.task_id)] = str(row.last_error)  # later rows overwrite: newest
    return causes


def _waiting_reason(*, cause: str | None, open_question: str | None) -> tuple[AttentionReason, str]:
    """Why this task waits, and the one line to show for it."""
    if cause:
        return AttentionReason.STUCK, cause
    if open_question:
        return AttentionReason.QUESTION, open_question
    return AttentionReason.WAITING, ""


def list_attention(engine: Engine, *, owner_id: str, now: datetime) -> list[AttentionItem]:
    """Everything that needs ``owner_id`` right now, in triage order (D-W1-5 / D-W1-6).

    Args:
        engine: The owner-scoped RLS engine.
        owner_id: The caller.
        now: The read instant (unused for filtering today; carried so a future window is
            clock-injected, never ambient).
    """
    del now  # reserved: the FAILED window is count-bounded today, not time-bounded
    reader = APITaskStateReader(TaskStore(engine), CheckpointStore(engine), owner_id)
    approvals = ApprovalStore(engine).list_pending_for_owner(owner_id)
    items: list[AttentionItem] = [
        AttentionItem(
            kind=AttentionKind.APPROVAL,
            reason=AttentionReason.APPROVAL,
            persona_id=p.persona_id,
            task_id=p.task_id,
            proposal_id=p.proposal_id,
            title=p.description,
            actions=_ACTIONS[(AttentionKind.APPROVAL, AttentionReason.APPROVAL)],
            since=p.created_at,
        )
        for p in approvals
    ]
    tasks_with_approval = {p.task_id for p in approvals}

    waiting = sorted(
        (
            t
            for t in reader.list_active()
            if t.state is TaskState.WAITING
            and t.wait_kind is WaitKind.ON_USER
            and t.id not in tasks_with_approval
        ),
        key=lambda t: t.updated_at,
    )
    causes = _dead_leg_causes(engine, owner_id, [t.id for t in waiting])
    for task in waiting:
        checkpoint = reader.get_latest_checkpoint(task.id)
        blocked = checkpoint.blocked_on if checkpoint is not None else None
        # Spec W1 (D-W1-35): the question the task is parked on is the LAST one asked, not
        # the first one it ever asked. A reply clears what it answered, so at a parked head
        # this is normally the only entry; taking the newest keeps the line honest even if an
        # older one lingers, because asking the user to answer a question they already
        # answered is worse than saying nothing.
        question = (
            checkpoint.open_questions[-1]
            if checkpoint is not None and checkpoint.open_questions
            else None
        )
        reason, detail = _waiting_reason(
            cause=causes.get(task.id) or blocked, open_question=question
        )
        items.append(
            AttentionItem(
                kind=AttentionKind.WAITING_ON_USER,
                reason=reason,
                persona_id=task.persona_id,
                task_id=task.id,
                proposal_id=None,
                title=task.contract.goal,
                detail=detail,
                actions=_ACTIONS[(AttentionKind.WAITING_ON_USER, reason)],
                since=task.updated_at,
            )
        )

    failed = [
        t for t in reader.list_recent_terminal(limit=TERMINAL_WINDOW) if t.state is TaskState.FAILED
    ]
    failed_causes = _dead_leg_causes(engine, owner_id, [t.id for t in failed])
    for task in failed:
        checkpoint = reader.get_latest_checkpoint(task.id)
        blocked = checkpoint.blocked_on if checkpoint is not None else None
        items.append(
            AttentionItem(
                kind=AttentionKind.FAILED,
                reason=AttentionReason.STUCK,
                persona_id=task.persona_id,
                task_id=task.id,
                proposal_id=None,
                title=task.contract.goal,
                detail=failed_causes.get(task.id) or blocked or "",
                actions=_ACTIONS[(AttentionKind.FAILED, AttentionReason.STUCK)],
                since=task.updated_at,
            )
        )
    return items


def count_attention(engine: Engine, *, owner_id: str, now: datetime) -> int:
    """The badge number: the length of the same list the review page renders."""
    return len(list_attention(engine, owner_id=owner_id, now=now))
