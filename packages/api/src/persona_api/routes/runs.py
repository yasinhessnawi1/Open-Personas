"""Agentic run routes (spec 08, T11, §5.3, KEYSTONE 2).

start / status / events (SSE) / respond / cancel. Every route is RLS-scoped via
``get_current_user``. The run executes as a background task (the ``RunRegistry``
on ``app.state``); ``/runs`` is rate-limited (5/min, §6).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse
from persona.tasks import ContractAttachment, is_terminal

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rate_limit import rate_limit
from persona_api.routes._runtime_guard import require_runtime_wired
from persona_api.schemas import (
    RespondToRunRequest,
    RunListResponse,
    RunStatusResponse,
    StartRunRequest,
    WorkDispatchResponse,
)
from persona_api.services import (
    audit_service,
    run_service,
    task_control_service,
    work_dispatch_service,
)
from persona_api.tasks.store import TaskStore

router = APIRouter(prefix="/v1", tags=["runs"])


@router.post(
    "/personas/{persona_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WorkDispatchResponse,
    dependencies=[Depends(rate_limit("runs"))],
)
async def start_run(
    persona_id: str,
    body: StartRunRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WorkDispatchResponse:
    """Dispatch a one-off: an ad hoc task whose first leg the worker runs (Spec W1, D-W1-1).

    The path and the pre-flight guards are unchanged; what starts is a task, not an
    in-process run, so the one-off gets checkpoints, continuation and the category gate by
    construction. The response names the task; its runs appear on the task detail as the
    worker opens them.
    """
    # Pre-flight credit guard (D-11-12 / spec 11 §5): 402 before anything is written.
    request.app.state.credits_policy.require_credits(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )
    # Pre-flight runtime guard: a keyless/unwired boot (no model configured) →
    # clean 503 before a task is created that no leg could ever run (R1-D-2).
    require_runtime_wired(request, "build_agentic_loop")
    engine = request.app.state.rls_engine
    dispatched = work_dispatch_service.dispatch_ad_hoc(
        rls_engine=engine,
        queue=JobQueue(engine),
        owner_id=user.id,
        persona_id=persona_id,
        brief=body.task,
        # Issue #16: the files the person attached on the hand-off dialog travel with the
        # ask, so the first leg opens them instead of working from the sentence alone.
        attachments=[
            ContractAttachment(ref=a.ref, filename=a.filename, media_type=a.media_type)
            for a in body.attachments
        ],
    )
    audit_service.record(
        engine=engine,
        user_id=user.id,
        action="task.dispatch",
        target=dispatched.task_id,
    )
    return WorkDispatchResponse(
        task_id=dispatched.task_id,
        persona_id=dispatched.persona_id,
        kind=dispatched.kind,
        state=dispatched.state,
    )


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> RunListResponse:
    """List the caller's runs, newest first (RLS-scoped). Backs the Tasks page."""
    rows = run_service.list_runs(rls_engine=request.app.state.rls_engine)
    return RunListResponse(items=[run_service.summarise_run(r) for r in rows])


@router.get("/runs/{run_id}", response_model=RunStatusResponse)
async def get_run(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> RunStatusResponse:
    """Get a run's status + accumulated steps (RLS-scoped → 404)."""
    row = run_service.get_run(rls_engine=request.app.state.rls_engine, run_id=run_id)
    return _run_status(row)


@router.get("/runs/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> StreamingResponse:
    """Stream the run's events as SSE: the in-process bus, or a tail of the run row (W1)."""
    # Ownership check: 404 if the run isn't the caller's.
    run_service.get_run(rls_engine=request.app.state.rls_engine, run_id=run_id)
    generator = run_service.stream_run_events(
        registry=request.app.state.run_registry,
        run_id=run_id,
        rls_engine=request.app.state.rls_engine,
    )
    return StreamingResponse(generator, media_type="text/event-stream")


@router.post("/runs/{run_id}/respond", status_code=status.HTTP_204_NO_CONTENT)
async def respond(
    run_id: str,
    body: RespondToRunRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> None:
    """Deliver an answer to a run awaiting an ask-user question."""
    run_service.respond_to_run(
        rls_engine=request.app.state.rls_engine,
        registry=request.app.state.run_registry,
        run_id=run_id,
        answer=body.answer,
    )


@router.post("/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, str]:
    """Cancel from the run viewer: the run's TASK when it has one, else the in-process run.

    Spec W1 (D-W1-1 / D-W1-14): one control set. A run that belongs to a task is one of its
    executions, so cancelling it cancels the task (terminal, schedule stopped); a legacy
    in-process run still flips its own token. A running leg finishes its current step and
    then stops (the external cancel seam is T6, D-W1-21).
    """
    engine = request.app.state.rls_engine
    row = run_service.get_run(rls_engine=engine, run_id=run_id)
    task_id = row.get("task_id")
    if task_id is not None:
        task = TaskStore(engine).get(user.id, str(task_id))
        if is_terminal(task.state):  # already done / cancelled: the durable truth, calmly
            return {"status": "finished", "task_id": str(task_id)}
        task_control_service.cancel_task(engine, user.id, task)
        return {"status": "cancelling", "task_id": str(task_id)}
    run_service.cancel_run(
        rls_engine=engine,
        registry=request.app.state.run_registry,
        run_id=run_id,
    )
    audit_service.record(
        engine=engine,
        user_id=user.id,
        action="run.cancel",
        target=run_id,
    )
    return {"status": "cancelling"}


def _run_status(row: dict[str, object]) -> RunStatusResponse:
    task_id = row.get("task_id")
    return RunStatusResponse(
        id=str(row["id"]),
        persona_id=str(row["persona_id"]),
        task=str(row["task"]),
        task_id=str(task_id) if task_id is not None else None,
        status=str(row["status"]),
        steps=run_service.steps_json(row),
        output=row.get("output"),  # type: ignore[arg-type]
        error=row.get("error"),  # type: ignore[arg-type]
    )
