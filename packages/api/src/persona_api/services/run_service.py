"""Agentic run lifecycle (spec 08, T11, §5.3, KEYSTONE 2).

Start / status / respond / cancel for agentic runs, decoupled from FastAPI. A
run is launched as a background ``asyncio.Task`` via the :class:`RunRegistry`
(``background/run_worker.py``); events flow to a per-run queue (SSE ``/events``),
``/respond`` pushes to the response queue the loop awaits, ``/cancel`` flips the
``CancelToken`` (D-08-5).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.errors import PersonaNotFoundError
from sqlalchemy import insert, select

from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.errors import RunNotFoundError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona_runtime.agentic.loop import AgenticLoop
    from sqlalchemy import Engine

    from persona_api.background.run_worker import RunRegistry

    LoopBuilder = Callable[[str], Awaitable[AgenticLoop]]

__all__ = [
    "cancel_run",
    "get_run",
    "respond_to_run",
    "start_run",
    "stream_run_events",
]


async def start_run(
    *,
    rls_engine: Engine,
    registry: RunRegistry,
    loop_builder: LoopBuilder,
    owner_id: str,
    persona_id: str,
    task: str,
    max_concurrent_long_ops: int = 0,
) -> str:
    """Insert the run row (status=running), build the loop, launch the task.

    Returns the ``run_id`` immediately (§5.3 — the run executes in the
    background). The ``runs`` row is committed first so ``/events`` / ``/respond``
    on the new id find it.
    """
    run_id = f"run_{uuid.uuid4().hex}"

    # Spec R7 (R7-D-4): reserve the durable per-user long-op slot BEFORE the run row
    # is persisted, so an over-cap user gets a clean 429 before a ``running`` row is
    # written. Released in the worker's terminal ``finally`` (all paths); if any setup
    # step below raises before hand-off, we release here so the slot never leaks.
    # ``max_concurrent_long_ops <= 0`` (community/uncapped) always admits (no DB write).
    from persona.concurrency import admit_long_op, release_long_op  # noqa: PLC0415

    op_token = admit_long_op(
        rls_engine=rls_engine,
        user_id=owner_id,
        op_class="agentic",
        max_concurrent=max_concurrent_long_ops,
    )
    if op_token is None:
        from persona_api.errors import ConcurrencyCappedError  # noqa: PLC0415

        raise ConcurrencyCappedError(
            "too many concurrent agentic runs in flight for this user",
            context={"user_id": owner_id, "retry_after_s": "5"},
        )
    try:
        with rls_engine.begin() as conn:
            if (
                conn.execute(select(personas_t.c.id).where(personas_t.c.id == persona_id)).first()
                is None
            ):
                raise PersonaNotFoundError("persona not found", context={"id": persona_id})
            conn.execute(
                insert(runs_t).values(
                    id=run_id,
                    owner_id=owner_id,
                    persona_id=persona_id,
                    task=task,
                    status="running",
                    started_at=datetime.now(UTC),
                )
            )
        # Spec P4-D-3 — bind the run's sandbox context for the loop build only, so the
        # builtin ``filesystem`` MCP subprocess is scoped at SPAWN from the same source
        # as the in-process file tools. The run uses ``run_id`` as the conversation_id
        # slot, EXACTLY as run_worker re-binds it for execution (owner_id:run_id), so
        # spawn-time and turn-time scope agree. Tight set→try→reset; no leak past build.
        from persona_api.sandbox import (  # noqa: PLC0415
            SandboxRequestContext,
            reset_sandbox_request_context,
            set_sandbox_request_context,
        )

        _scope_token = set_sandbox_request_context(
            SandboxRequestContext(owner_id=owner_id, conversation_id=run_id)
        )
        try:
            loop = await loop_builder(persona_id)
        finally:
            reset_sandbox_request_context(_scope_token)
        registry.start(
            run_id=run_id, owner_id=owner_id, loop=loop, task_text=task, op_token=op_token
        )
    except BaseException:
        # Any failure BEFORE the worker took ownership (persona 404 / persist / build)
        # → release the slot so a failed start never leaks it. On success the worker's
        # ``finally`` owns the release.
        release_long_op(rls_engine=rls_engine, user_id=owner_id, op_id=op_token)
        raise
    return run_id


def get_run(*, rls_engine: Engine, run_id: str) -> dict[str, object]:
    """Return a run's row (status + steps), RLS-scoped → 404 if not the caller's."""
    with rls_engine.begin() as conn:
        row = conn.execute(select(runs_t).where(runs_t.c.id == run_id)).mappings().first()
    if row is None:
        raise RunNotFoundError("run not found", context={"id": run_id})
    return dict(row)


def list_runs(*, rls_engine: Engine, limit: int = 100) -> list[dict[str, object]]:
    """Return the caller's runs (RLS-scoped), newest first.

    Spec 35: backs the Tasks page index. The heavy ``steps`` JSON is excluded
    from the projection — the list only needs task / status / persona / timing;
    the per-run viewer (:func:`get_run`) loads the full row.
    """
    cols = (
        runs_t.c.id,
        runs_t.c.persona_id,
        runs_t.c.task,
        runs_t.c.status,
        runs_t.c.started_at,
        runs_t.c.finished_at,
    )
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(select(*cols).order_by(runs_t.c.started_at.desc()).limit(limit))
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def respond_to_run(*, rls_engine: Engine, registry: RunRegistry, run_id: str, answer: str) -> None:
    """Deliver a user's answer to a run awaiting an ask-user question.

    Verifies the run is the caller's (RLS), then pushes to the in-process
    response queue the loop's ``user_respond`` callback awaits.
    """
    _require_owned(rls_engine, run_id)
    handle = registry.get(run_id)
    if handle is None:
        raise RunNotFoundError("run is not active", context={"id": run_id})
    handle.responses.put_nowait(answer)


def cancel_run(*, rls_engine: Engine, registry: RunRegistry, run_id: str) -> None:
    """Cancel a running run: flip its CancelToken (the loop stops at the next
    step boundary → status ``cancelled``, D-06-7)."""
    _require_owned(rls_engine, run_id)
    handle = registry.get(run_id)
    if handle is None:
        raise RunNotFoundError("run is not active", context={"id": run_id})
    handle.cancel_token.cancel()


async def stream_run_events(*, registry: RunRegistry, run_id: str) -> AsyncIterator[bytes]:
    """SSE generator: drain the run's event queue until the end-of-stream sentinel.

    Each ``RunEvent`` serialises via ``model_dump_json`` straight into the SSE
    ``data`` field (spec 06 handoff). The run must be active (in the registry);
    a completed run's events can be reconstructed from ``runs.steps`` via
    :func:`get_run` (catch-up — D-08-5).
    """
    handle = registry.get(run_id)
    if handle is None:
        raise RunNotFoundError("run is not active", context={"id": run_id})
    while True:
        event = await handle.events.get()
        if event is None:  # end-of-stream sentinel
            break
        yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n".encode()
    yield b"event: end\ndata: {}\n\n"


def _require_owned(rls_engine: Engine, run_id: str) -> None:
    """Raise RunNotFoundError unless the run is visible to the caller (RLS)."""
    with rls_engine.begin() as conn:
        if conn.execute(select(runs_t.c.id).where(runs_t.c.id == run_id)).first() is None:
            raise RunNotFoundError("run not found", context={"id": run_id})


def steps_json(row: dict[str, object]) -> list[dict[str, object]]:
    """Coerce the runs.steps JSONB column to a list of dicts for the response."""
    steps = row.get("steps")
    if steps is None:
        return []
    if isinstance(steps, str):
        loaded = json.loads(steps)
        return list(loaded) if isinstance(loaded, list) else []
    return list(steps) if isinstance(steps, list) else []
