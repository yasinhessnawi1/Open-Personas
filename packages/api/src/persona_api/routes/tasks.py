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
from persona.errors import ScheduleNotFoundError, TaskNotFoundError
from persona.logging import get_logger
from persona.tasks import TaskState, format_micros, is_terminal
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
from persona_api.errors import WorkBriefTooLongError
from persona_api.jobs.queue import JobQueue
from persona_api.schedules.store import ScheduleStore
from persona_api.schemas.requests import BudgetExtendRequest, TaskReplyRequest
from persona_api.schemas.responses import (
    AcceptanceCriterionOut,
    BudgetExtendResult,
    BudgetOut,
    GrantOut,
    LedgerOut,
    ScheduleCadenceOut,
    TaskAuditEntryOut,
    TaskCheckpointOut,
    TaskCommandResult,
    TaskDetailOut,
    TaskReportOut,
    TaskSummaryOut,
)
from persona_api.services import (
    audit_service,
    calendar_reschedule_service,
    run_service,
    task_control_service,
)
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.reader import APITaskStateReader
from persona_api.tasks.store import CheckpointStore, TaskStore
from persona_api.textline import DETAIL_BUDGET, full_text, one_line

if TYPE_CHECKING:
    from collections.abc import Iterable

    from persona.tasks import Task, TaskCheckpoint
    from sqlalchemy import Engine

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])

_LIST_LIMIT = 50
#: A reply rides into the next leg's trigger block verbatim (D-W1-7's cap, D-W1-28's sentence).
MAX_REPLY_CHARS = 8_000
REPLY_TOO_LONG_MESSAGE = (
    "That reply is longer than I can take in one go. Keep it under 8,000 characters."
)
_CHECKPOINT_LIMIT = 10
#: A single budget extension can't exceed the platform default cap (the bound — never unbounded).
_MAX_EXTEND_MICROS = PLATFORM_DEFAULT_BUDGET_MICROS


def _extension_ceiling_detail() -> str:
    """The 422 a user earns by asking for too much, in the currency they are charged in.

    R9-172 tail: this said "the maximum of 100000 micros", which is the ledger's own integer
    and means nothing to the person reading it. The web surfaces this detail on a failed
    raise-the-cap, so it is user-visible text, not an operator log line.
    """
    return f"that is more than you can add at once, which is {format_micros(_MAX_EXTEND_MICROS)}"


_log = get_logger("api.routes.tasks")


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


# Spec W1 (D-W1-14): the mirror now lives with the task controls; kept under its old name
# here for the pause/resume routes (and the R9-108 test that imports it from this module).
_mirror_schedule_pause = task_control_service.mirror_schedule_pause


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


def _clean(text: str) -> str:
    """A6-D-4: progress / next-step / causes are SUMMARY lines, never raw
    transcripts — but executors sometimes store the full delivery message
    (voice-markup, markdown, paragraphs). Flatten at the read seam so stored
    rows stay verbose while every surface reads one calm line (R11-B2 rider)."""
    return one_line(text, DETAIL_BUDGET)


def _clean_opt(text: str | None) -> str | None:
    return None if text is None else _clean(text)


def _clean_list(values: Iterable[str]) -> list[str]:
    return [_clean(v) for v in values]


def _summary(
    task: Task, budget: BudgetEnforcer, owner_id: str, *, stuck_cause: str | None = None
) -> TaskSummaryOut:
    return TaskSummaryOut(
        task_id=task.id,
        persona_id=task.persona_id,
        goal=task.contract.goal,
        kind=task.kind.value,
        status=summarise_task(task).status.value,
        paused=task.paused,
        spent_micros=task.ledger.total_micros,
        budget_cap_micros=budget.effective_cap(owner_id, task),
        updated_at=task.updated_at,
        # A6-D-5's loud card shows a CAUSE line, not a transcript (R11-B2 rider).
        stuck_cause=_clean_opt(stuck_cause),
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


def _schedule_cadence(
    engine: Engine, owner_id: str, schedule_id: str | None
) -> ScheduleCadenceOut | None:
    """The backing schedule's cadence in picker vocabulary, or ``None`` when there is none.

    R9-178: what the Reschedule dialog seeds its builder from. A dangling ``schedule_id`` (the
    row was deleted under the task) reads as "no schedule" rather than failing the whole detail;
    the task's own fields are still worth showing.
    """
    if schedule_id is None:
        return None
    try:
        schedule = ScheduleStore(engine).get(owner_id, schedule_id)
    except ScheduleNotFoundError:
        _log.warning(
            "task detail: backing schedule not found; cadence omitted schedule_id={sid}",
            sid=schedule_id,
        )
        return None
    return calendar_reschedule_service.current_cadence(schedule)


def _report(task: Task, checkpoint: TaskCheckpoint | None, now: datetime) -> TaskReportOut | None:
    """The terminal outcome as its distinct projection (a failure never renders as success).

    The report is where the COMPLETE outcome is read (owner-ruled, R11-B2) — every field
    keeps its full text, markup-cleaned but never clipped; only the summary projections
    (checkpoints, causes at list level, progress) flatten through the clamped `_clean`.
    """
    if task.state is TaskState.COMPLETED:
        completion = build_completion_report(task, checkpoint, now=now)
        return TaskReportOut(
            kind="completed", conclusions=[full_text(c) for c in completion.conclusions]
        )
    if task.state is TaskState.FAILED:
        cause = (checkpoint.blocked_on if checkpoint is not None else None) or ""
        stuck = build_stuck_report(task, checkpoint, cause=cause, now=now)
        return TaskReportOut(
            kind="stuck",
            cause=full_text(stuck.cause),
            where_it_stood=[full_text(w) for w in stuck.where_it_stood],
            next_step=full_text(stuck.next_step),
        )
    if task.state is TaskState.CANCELLED:
        cancelled = build_cancellation_summary(task, checkpoint, now=now)
        return TaskReportOut(
            kind="cancelled",
            where_it_stood=[full_text(w) for w in cancelled.where_it_stood],
            next_step=full_text(cancelled.next_step),
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
        kind=task.kind.value,
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
        progress=_clean_list(view.progress),
        next_step=_clean(view.next_step),
        open_questions=_clean_list(view.open_questions),
        wait_reason=_clean_opt(view.wait_reason),
        report=_report(task, checkpoint, now),
        checkpoints=[
            TaskCheckpointOut(
                seq=c.checkpoint_seq,
                progress_conclusions=_clean_list(c.progress_conclusions),
                next_step=_clean(c.next_step),
                open_questions=_clean_list(c.open_questions),
                blocked_on=_clean_opt(c.blocked_on),
                updated_at=c.updated_at,
            )
            for c in recent
        ],
        conversation_id=task.conversation_id,
        schedule_id=task.schedule_id,
        schedule_cadence=_schedule_cadence(engine, user.id, task.schedule_id),
        run_ids=list(task.run_ids),
        # Spec W1 (D-W1-3): the task detail is the home of its run history.
        runs=[
            run_service.summarise_run(r)
            for r in run_service.list_runs_for_task(rls_engine=engine, task_id=task_id)
        ],
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
    # Spec W1 (R9-146): the ONE pause every door shares (this route, the chat verb).
    outcome = task_control_service.pause_task(engine, user.id, task, now=datetime.now(UTC))
    return _command_result(store.get(user.id, task_id), changed=outcome.changed, note=outcome.note)


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
    # Spec W1 (R9-146, D-W1-30): the ONE resume every door shares. It clears the overlay AND
    # puts the next leg back on the worker from the salvaged head; the chat verb rides the
    # same function through ``TaskControlMutator``.
    outcome = task_control_service.resume_task(engine, user.id, task, now=datetime.now(UTC))
    return _command_result(
        store.get(user.id, task_id),
        changed=outcome.changed,
        note=outcome.note,
        owner_paused=outcome.owner_paused,
    )


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
    # Spec W1 (D-W1-14): the ONE cancel every door shares (task detail, review, run viewer).
    task_control_service.cancel_task(engine, user.id, task, now=datetime.now(UTC))
    return _command_result(
        store.get(user.id, task_id),
        changed=True,
        note="A running step finishes its current work, then the task stops.",
    )


@router.post("/{task_id}/pickup", response_model=TaskCommandResult)
async def pickup_task(
    task_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TaskCommandResult:
    """Pick up a task that waits on you (Spec W1, T6): its next leg runs on the worker.

    Cross-tenant ids are not found (RLS). A paused task, a paused owner or a suspended persona
    is refused with the reason (D-W1-10). Idempotent: a second pickup dedups to the one job.
    """
    engine = request.app.state.rls_engine
    store = TaskStore(engine)
    try:
        task = store.get(user.id, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    outcome = task_control_service.pickup_task(engine, user.id, task, now=datetime.now(UTC))
    if outcome.changed:
        audit_service.record(engine=engine, user_id=user.id, action="task.pickup", target=task_id)
    return _command_result(
        store.get(user.id, task_id),
        changed=outcome.changed,
        note=outcome.note,
        owner_paused=outcome.owner_paused,
    )


@router.post("/{task_id}/reply", response_model=TaskCommandResult)
async def reply_to_task(
    task_id: str,
    body: TaskReplyRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TaskCommandResult:
    """Answer a task that waits on you (Spec W1, T6; D-W1-4): the reply rides into its next leg.

    A task whose wait is a pending approval answers 409 and names the approval.
    """
    engine = request.app.state.rls_engine
    store = TaskStore(engine)
    try:
        task = store.get(user.id, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    reply = body.reply.strip()
    if len(reply) > MAX_REPLY_CHARS:
        raise WorkBriefTooLongError(
            REPLY_TOO_LONG_MESSAGE,
            context={"max_chars": str(MAX_REPLY_CHARS), "chars": str(len(reply))},
        )
    outcome = task_control_service.reply_to_task(
        engine, user.id, task, reply, now=datetime.now(UTC)
    )
    if outcome.changed:
        audit_service.record(engine=engine, user_id=user.id, action="task.reply", target=task_id)
    return _command_result(
        store.get(user.id, task_id),
        changed=outcome.changed,
        note=outcome.note,
        owner_paused=outcome.owner_paused,
    )


@router.post("/{task_id}/retry", response_model=TaskCommandResult)
async def retry_task(
    task_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TaskCommandResult:
    """Run a finished task again as a NEW task with the same contract (Spec W1, T6).

    The response names the successor; the old task stays as the record of what happened.
    """
    engine = request.app.state.rls_engine
    store = TaskStore(engine)
    try:
        task = store.get(user.id, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail="task not found") from exc
    outcome = task_control_service.retry_task(engine, user.id, task, now=datetime.now(UTC))
    if outcome.successor is not None:
        audit_service.record(
            engine=engine, user_id=user.id, action="task.retry", target=outcome.successor.id
        )
    result = _command_result(
        task, changed=outcome.changed, note=outcome.note, owner_paused=outcome.owner_paused
    )
    return result.model_copy(
        update={"successor_task_id": outcome.successor.id if outcome.successor else None}
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
        raise HTTPException(status_code=422, detail=_extension_ceiling_detail())
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
        note="" if applied else "This task isn't paused at its budget cap: nothing to extend.",
    )


__all__ = ["router"]
