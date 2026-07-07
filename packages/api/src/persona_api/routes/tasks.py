"""The tasks read surface (Spec A6, B1) — the list, the detail, the audit trail.

Thin, RLS-scoped reads over the existing A2/A3 stores (no new mechanism): the cross-persona list
with the state matrix + spend, the task detail above the run viewer (contract + grants visible,
ledger, budget, the terminal report, checkpoints as the human "where it is"), and the readable
audit trail. Every read re-binds the owner's GUC through the store, so a cross-tenant reach is
empty.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from persona.errors import TaskNotFoundError, TaskStateError
from persona.tasks import TaskState, is_terminal
from persona.tasks.reader import IntrospectionStatus, project_task_state, summarise_task
from persona.tasks.reports import (
    build_cancellation_summary,
    build_completion_report,
    build_stuck_report,
)
from persona.tools import ActionCategory
from sqlalchemy import select

from persona_api.approvals import KillSwitchStore
from persona_api.approvals.budget import PLATFORM_DEFAULT_BUDGET_MICROS, BudgetEnforcer
from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.db.engine import rls_connection
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import task_checkpoints as checkpoints_t
from persona_api.jobs.queue import JobQueue
from persona_api.schedules.store import ScheduleStore
from persona_api.schemas.requests import BudgetExtendRequest
from persona_api.schemas.responses import (
    AcceptanceCriterionOut,
    BudgetExtendResult,
    BudgetOut,
    GrantOut,
    LedgerOut,
    TaskAuditEntryOut,
    TaskCheckpointOut,
    TaskCommandResult,
    TaskDetailOut,
    TaskReportOut,
    TaskSummaryOut,
)
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.reader import APITaskStateReader
from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from persona.tasks import Task, TaskCheckpoint
    from sqlalchemy import Engine

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

_LIST_LIMIT = 50
_CHECKPOINT_LIMIT = 10
#: A single budget extension can't exceed the platform default cap (the bound — never unbounded).
_MAX_EXTEND_MICROS = PLATFORM_DEFAULT_BUDGET_MICROS


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


def _command_result(
    task: Task, *, changed: bool, note: str = "", owner_paused: bool = False
) -> TaskCommandResult:
    return TaskCommandResult(
        task_id=task.id,
        status=summarise_task(task).status.value,
        paused=task.paused,
        changed=changed,
        owner_autonomy_paused=owner_paused,
        note=note,
    )


#: The derived statuses that make a task "stuck" (A6-D-5) — the only rows that carry a cause.
_STUCK_STATUSES = frozenset({IntrospectionStatus.WAITING_ON_USER, IntrospectionStatus.FAILED})


def _summary(
    task: Task, budget: BudgetEnforcer, owner_id: str, *, stuck_cause: str | None = None
) -> TaskSummaryOut:
    return TaskSummaryOut(
        task_id=task.id,
        persona_id=task.persona_id,
        goal=task.contract.goal,
        status=summarise_task(task).status.value,
        paused=task.paused,
        spent_micros=task.ledger.total_micros,
        budget_cap_micros=budget.effective_cap(owner_id, task),
        updated_at=task.updated_at,
        stuck_cause=stuck_cause,
    )


def _stuck_causes(engine: Engine, owner_id: str, task_ids: list[str]) -> dict[str, str]:
    """The head checkpoint's ``blocked_on`` per stuck task, in ONE query (no N; A6-D-5).

    ``DISTINCT ON (task_id) … ORDER BY task_id, checkpoint_seq DESC`` reads exactly the head
    checkpoint of each task in the (small) stuck subset; ``blocked_on`` lives in the checkpoint
    JSON. RLS-scoped; a task with no checkpoint or a null reason simply has no entry (→ ``None``
    at the summary, an honestly thin loud card).
    """
    if not task_ids:
        return {}
    blocked_on = checkpoints_t.c.checkpoint_json["blocked_on"].as_string()
    with rls_connection(engine, owner_id) as conn:
        rows = conn.execute(
            select(checkpoints_t.c.task_id, blocked_on.label("cause"))
            .where(checkpoints_t.c.task_id.in_(task_ids), blocked_on.isnot(None))
            .distinct(checkpoints_t.c.task_id)
            .order_by(checkpoints_t.c.task_id, checkpoints_t.c.checkpoint_seq.desc())
        ).all()
    return {r.task_id: r.cause for r in rows}


def _report(task: Task, checkpoint: TaskCheckpoint | None, now: datetime) -> TaskReportOut | None:
    """The terminal outcome as its distinct projection (a failure never renders as success)."""
    if task.state is TaskState.COMPLETED:
        completion = build_completion_report(task, checkpoint, now=now)
        return TaskReportOut(kind="completed", conclusions=list(completion.conclusions))
    if task.state is TaskState.FAILED:
        cause = (checkpoint.blocked_on if checkpoint is not None else None) or ""
        stuck = build_stuck_report(task, checkpoint, cause=cause, now=now)
        return TaskReportOut(
            kind="stuck",
            cause=stuck.cause,
            where_it_stood=list(stuck.where_it_stood),
            next_step=stuck.next_step,
        )
    if task.state is TaskState.CANCELLED:
        cancelled = build_cancellation_summary(task, checkpoint, now=now)
        return TaskReportOut(
            kind="cancelled",
            where_it_stood=list(cancelled.where_it_stood),
            next_step=cancelled.next_step,
        )
    return None


@router.get("", response_model=list[TaskSummaryOut])
async def list_tasks(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[TaskSummaryOut]:
    """Standing + recent tasks across the caller's personas — state, spend, paused (the matrix)."""
    engine = request.app.state.rls_engine
    reader = APITaskStateReader(TaskStore(engine), CheckpointStore(engine), user.id)
    budget = BudgetEnforcer(engine=engine, tasks=TaskStore(engine), queue=JobQueue(engine))
    tasks = [*reader.list_active(), *reader.list_recent_terminal(limit=_LIST_LIMIT)]
    # the stuck subset carries a cause at list level (A6-D-5) — derived in ONE query, not N.
    stuck_ids = [t.id for t in tasks if summarise_task(t).status in _STUCK_STATUSES]
    causes = _stuck_causes(engine, user.id, stuck_ids)
    return [_summary(t, budget, user.id, stuck_cause=causes.get(t.id)) for t in tasks]


@router.get("/{task_id}", response_model=TaskDetailOut)
async def get_task(
    task_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TaskDetailOut:
    """The task detail — contract + grants, state, ledger, budget, terminal report, checkpoints."""
    engine = request.app.state.rls_engine
    task_store = TaskStore(engine)
    checkpoint_store = CheckpointStore(engine)
    try:
        task = task_store.get(user.id, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc

    now = datetime.now(UTC)
    checkpoint = checkpoint_store.get_latest(user.id, task_id)
    view = project_task_state(task, checkpoint)
    budget = BudgetEnforcer(engine=engine, tasks=task_store, queue=JobQueue(engine))
    contract = task.contract
    recent = checkpoint_store.list_recent(user.id, task_id, limit=_CHECKPOINT_LIMIT)

    return TaskDetailOut(
        task_id=task.id,
        persona_id=task.persona_id,
        goal=contract.goal,
        scope=contract.scope,
        status=view.status.value,
        paused=task.paused,
        grants=[
            GrantOut(category=c.value, decision=contract.category_policy.decide(c).value)
            for c in ActionCategory
        ],
        acceptance_criteria=[
            AcceptanceCriterionOut(id=a.id, statement=a.statement, status=a.status.value)
            for a in contract.acceptance_criteria
        ],
        deadline=contract.bounds.deadline,
        max_legs=contract.bounds.max_legs,
        budget=BudgetOut(
            cap_micros=budget.effective_cap(user.id, task),
            spent_micros=task.ledger.total_micros,
            state=budget.check(user.id, task).value,
        ),
        ledger=LedgerOut(
            model_micros=task.ledger.model_micros,
            sandbox_micros=task.ledger.sandbox_micros,
            external_micros=task.ledger.external_micros,
            total_micros=task.ledger.total_micros,
        ),
        progress=list(view.progress),
        next_step=view.next_step,
        open_questions=list(view.open_questions),
        wait_reason=view.wait_reason,
        report=_report(task, checkpoint, now),
        checkpoints=[
            TaskCheckpointOut(
                seq=c.checkpoint_seq,
                progress_conclusions=list(c.progress_conclusions),
                next_step=c.next_step,
                open_questions=list(c.open_questions),
                blocked_on=c.blocked_on,
                updated_at=c.updated_at,
            )
            for c in recent
        ],
        conversation_id=task.conversation_id,
        schedule_id=task.schedule_id,
        run_ids=list(task.run_ids),
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


@router.get("/{task_id}/audit", response_model=list[TaskAuditEntryOut])
async def get_task_audit(
    task_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[TaskAuditEntryOut]:
    """The task's audit trail — budget events, lifecycle, provenance (A3's records, readable)."""
    engine = request.app.state.rls_engine
    try:
        TaskStore(engine).get(user.id, task_id)  # 404 for a missing/foreign task
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    with rls_connection(engine, user.id) as conn:
        rows = (
            conn.execute(
                select(audit_log_t)
                .where(audit_log_t.c.user_id == user.id, audit_log_t.c.target == task_id)
                .order_by(audit_log_t.c.created_at.asc())
            )
            .mappings()
            .all()
        )
    return [
        TaskAuditEntryOut(
            action=str(r["action"]),
            target=str(r["target"]),
            metadata=dict(r["metadata"]),
            created_at=r["created_at"],
        )
        for r in rows
    ]


# --- commands (B2) — mutations: audited, idempotent, bounded ---------------------------------


@router.post("/{task_id}/pause", response_model=TaskCommandResult)
async def pause_task(
    task_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TaskCommandResult:
    """Pause a task (no new legs). Idempotent — pausing an already-paused task is a calm no-op."""
    engine = request.app.state.rls_engine
    store = TaskStore(engine)
    try:
        task = store.get(user.id, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    if task.paused:
        return _command_result(task, changed=False, note="Already paused.")
    try:
        updated = store.pause(user.id, task_id, now=datetime.now(UTC))  # audits task.pause
    except TaskStateError:  # terminal → nothing to pause; reflect the durable truth
        return _command_result(
            store.get(user.id, task_id), changed=False, note="This task has already finished."
        )
    return _command_result(updated, changed=True)


@router.post("/{task_id}/resume", response_model=TaskCommandResult)
async def resume_task(
    task_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TaskCommandResult:
    """Resume a paused task. If the owner's autonomy is paused, reflect it — never silently arm."""
    engine = request.app.state.rls_engine
    store = TaskStore(engine)
    try:
        task = store.get(user.id, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    if not task.paused:
        return _command_result(task, changed=False, note="Not paused.")
    updated = store.unpause(user.id, task_id, now=datetime.now(UTC))  # audits task.unpause
    owner_paused = _kill_switch(engine).is_owner_autonomy_paused(user.id)
    note = (
        "Resumed — but your autonomy is paused, so it won't run until you resume autonomy."
        if owner_paused
        else ""
    )
    return _command_result(updated, changed=True, note=note, owner_paused=owner_paused)


@router.post("/{task_id}/cancel", response_model=TaskCommandResult)
async def cancel_task(
    task_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TaskCommandResult:
    """Cancel a task (terminal) — a running step finishes its current work first, stated plainly."""
    engine = request.app.state.rls_engine
    store = TaskStore(engine)
    try:
        task = store.get(user.id, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    if is_terminal(task.state):  # already done/cancelled → the durable truth, no error
        return _command_result(task, changed=False, note="This task has already finished.")
    _kill_switch(engine).cancel_task(user.id, task_id, now=datetime.now(UTC))  # audits task.cancel
    return _command_result(
        store.get(user.id, task_id),
        changed=True,
        note="A running step finishes its current work, then the task stops.",
    )


@router.post("/{task_id}/budget/extend", response_model=BudgetExtendResult)
async def extend_budget(
    task_id: str,
    body: BudgetExtendRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> BudgetExtendResult:
    """Raise a budget-paused task's cap (bounded, at-most-once). Reports the old → new cap."""
    if body.amount_micros > _MAX_EXTEND_MICROS:
        raise HTTPException(
            status_code=422,
            detail=f"extension exceeds the maximum of {_MAX_EXTEND_MICROS} micros",
        )
    engine = request.app.state.rls_engine
    store = TaskStore(engine)
    try:
        task = store.get(user.id, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    budget = BudgetEnforcer(engine=engine, tasks=store, queue=JobQueue(engine))
    old_cap = budget.effective_cap(user.id, task)
    # At-most-once (the cas_unpause gate): only a budget-paused task extends; a lost race no-ops.
    applied = budget.extend(user.id, task_id, body.amount_micros, now=datetime.now(UTC))
    after = store.get(user.id, task_id)
    return BudgetExtendResult(
        task_id=task_id,
        applied=applied,
        old_cap_micros=old_cap,
        new_cap_micros=budget.effective_cap(user.id, after),
        state=budget.check(user.id, after).value,
        note="" if applied else "This task isn't paused at its budget cap — nothing to extend.",
    )


__all__ = ["router"]
