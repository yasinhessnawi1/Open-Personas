"""Agentic run lifecycle (spec 08, T11, §5.3, KEYSTONE 2).

Start / status / respond / cancel for agentic runs, decoupled from FastAPI. A
run is launched as a background ``asyncio.Task`` via the :class:`RunRegistry`
(``background/run_worker.py``); events flow to a per-run queue (SSE ``/events``),
``/respond`` pushes to the response queue the loop awaits, ``/cancel`` flips the
``CancelToken`` (D-08-5).
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from persona_runtime.agentic.events import RunEvent
from sqlalchemy import select

from persona_api.db.models import runs as runs_t
from persona_api.errors import RunNotFoundError
from persona_api.schemas import RunSummary

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy import Engine

    from persona_api.background.run_worker import RunRegistry


__all__ = [
    "cancel_run",
    "get_run",
    "list_runs_for_task",
    "summarise_run",
    "respond_to_run",
    "stream_run_events",
]


def get_run(*, rls_engine: Engine, run_id: str) -> dict[str, object]:
    """Return a run's row (status + steps), RLS-scoped → 404 if not the caller's."""
    with rls_engine.begin() as conn:
        row = conn.execute(select(runs_t).where(runs_t.c.id == run_id)).mappings().first()
    if row is None:
        raise RunNotFoundError("run not found", context={"id": run_id})
    return dict(row)


#: The light projection every run list shares: no ``steps`` JSON (the viewer loads that).
_SUMMARY_COLS = (
    runs_t.c.id,
    runs_t.c.persona_id,
    runs_t.c.task,
    runs_t.c.task_id,
    runs_t.c.status,
    runs_t.c.stop_reason,
    runs_t.c.started_at,
    runs_t.c.finished_at,
)


def summarise_run(row: dict[str, object]) -> RunSummary:
    """The light :class:`RunSummary` projection of a runs row (shared by every run list)."""
    task_id = row.get("task_id")
    stop_reason = row.get("stop_reason")
    return RunSummary(
        id=str(row["id"]),
        persona_id=str(row["persona_id"]),
        task=str(row["task"]),
        task_id=str(task_id) if task_id is not None else None,
        status=str(row["status"]),
        stop_reason=str(stop_reason) if stop_reason is not None else None,
        started_at=row["started_at"],  # type: ignore[arg-type]
        finished_at=row.get("finished_at"),  # type: ignore[arg-type]
    )


def list_runs(*, rls_engine: Engine, limit: int = 100) -> list[dict[str, object]]:
    """Return the caller's runs (RLS-scoped), newest first.

    Spec 35: backs the Tasks page index. The heavy ``steps`` JSON is excluded
    from the projection — the list only needs task / status / persona / timing;
    the per-run viewer (:func:`get_run`) loads the full row.
    """
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(select(*_SUMMARY_COLS).order_by(runs_t.c.started_at.desc()).limit(limit))
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def list_runs_for_task(
    *, rls_engine: Engine, task_id: str, limit: int = 50
) -> list[dict[str, object]]:
    """The task's runs (RLS-scoped), newest first, in the light projection (Spec W1, D-W1-3).

    Backs the task detail's run history: one indexed read on ``runs.task_id``.
    """
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(*_SUMMARY_COLS)
                .where(runs_t.c.task_id == task_id)
                .order_by(runs_t.c.started_at.desc())
                .limit(limit)
            )
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


#: Run statuses that are still in flight; anything else ends the DB tail.
#: Spec W1 (D-W1-34): ``awaiting_user`` is NOT one of them. A leg that stopped on a
#: question appends nothing further to that run — the answer arrives through the task's
#: reply route and opens a NEW run — so a tail that kept waiting on it would never end.
_LIVE_STATUSES: frozenset[str] = frozenset({"running"})

#: The DB tail's poll cadence: one primary-key read per second per open viewer.
DEFAULT_TAIL_POLL_SECONDS = 1.0


def _frame(event_type: str, payload: str) -> bytes:
    """One SSE frame, the exact shape the live path emits (``event:`` + ``data:``)."""
    return f"event: {event_type}\ndata: {payload}\n\n".encode()


def _stored_events(row: dict[str, object]) -> list[dict[str, object]]:
    """The run's persisted RunEvent dicts, in order.

    ``runs.steps`` holds the event log while the run is live (``persist_progress``) and the
    authoritative ``Step`` objects once it is final (``persist_final``). Only the former carry
    a ``timestamp``; the same filter the web viewer applies, so the tail never emits a Step
    as if it were an event.
    """
    return [s for s in steps_json(row) if isinstance(s, dict) and "timestamp" in s and "type" in s]


async def stream_run_events(
    *,
    registry: RunRegistry,
    run_id: str,
    rls_engine: Engine | None = None,
    poll_interval_seconds: float = DEFAULT_TAIL_POLL_SECONDS,
) -> AsyncIterator[bytes]:
    """SSE generator for a run: the in-process bus when it has one, else a tail of the row.

    Each ``RunEvent`` serialises straight into the SSE ``data`` field (spec 06 handoff),
    followed by a terminal ``end`` frame. Two sources, one frame shape:

    - **Registry** (an in-process run): drain its event queue to the end-of-stream sentinel.
    - **DB tail** (Spec W1, D-W1-1): a run executed by the worker has no registry handle,
      but its leg snapshots every event into ``runs.steps`` through the one runs writer.
      Poll the row (an owner-scoped primary-key read, off the event loop), emit each
      event not yet sent, and end when the status leaves :data:`_LIVE_STATUSES`. The
      poll stops the moment the client disconnects: Starlette closes the generator, the
      ``await`` raises ``CancelledError``, and there is no background task to leak.

    Raises:
        RunNotFoundError: No registry handle and no engine to tail (the pre-W1 contract).
    """
    handle = registry.get(run_id)
    if handle is not None:
        while True:
            event = await handle.events.get()
            if event is None:  # end-of-stream sentinel
                break
            yield _frame(event.type, event.model_dump_json())
        yield _frame("end", "{}")
        return
    if rls_engine is None:
        raise RunNotFoundError("run is not active", context={"id": run_id})
    sent = 0
    while True:
        row = await asyncio.to_thread(get_run, rls_engine=rls_engine, run_id=run_id)
        events = _stored_events(row)
        for event_dict in events[sent:]:
            # Re-hydrate and serialise through the SAME model the live path uses, so the
            # bytes on the wire are identical whichever source served them.
            event = RunEvent.model_validate(event_dict)
            yield _frame(event.type, event.model_dump_json())
        sent = max(sent, len(events))
        if str(row.get("status")) not in _LIVE_STATUSES:
            break
        await asyncio.sleep(poll_interval_seconds)
    yield _frame("end", "{}")


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
