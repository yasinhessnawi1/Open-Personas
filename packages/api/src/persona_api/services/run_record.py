"""The one writer of the durable ``runs`` record (Spec 08, T11, D-08-5).

A run is a row in ``runs`` regardless of *which* execution path produced it: the
interactive :class:`~persona_api.background.run_worker.RunRegistry` and the background
task leg (:mod:`persona_api.tasks.handler`) both persist through these functions. A
second writer is exactly how the two paths drifted apart — the leg executed a real
``AgenticLoop`` and left no ``runs`` row at all, so a scheduled run was invisible to the
run viewer — so the SQL lives here once and only once.

The contract is D-08-5's **viewable-not-resumable**: :func:`persist_progress` snapshots
the event log to ``runs.steps`` as it grows, so a crashed run stays inspectable; the
terminal write (:func:`persist_final`) then replaces it with the run's authoritative
steps plus ``status`` / ``output`` / ``error`` / ``finished_at``.

**Scoping.** ``owner_id`` binds the write to the run's tenant via
:func:`~persona_api.db.engine.rls_connection`. ``None`` means "use the ambient scope" —
the caller already bound ``app.current_user_id`` (the request middleware, or the run
worker's own contextvar bind for the run's lifetime). Background callers that run
outside any such scope MUST pass the owner explicitly.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError

from persona_api.db.engine import rls_connection
from persona_api.db.models import runs as runs_t
from persona_api.errors import RunPersonaOwnerMismatchError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from persona_runtime.agentic.run import Run
    from sqlalchemy import Connection, Engine

__all__ = [
    "insert_run",
    "persist_error",
    "persist_final",
    "persist_progress",
    "persist_terminal",
]


@contextmanager
def _scoped(engine: Engine, owner_id: str | None) -> Iterator[Connection]:
    """A transaction scoped to ``owner_id``, or to the ambient scope when ``None``."""
    if owner_id is None:
        with engine.begin() as conn:
            yield conn
    else:
        with rls_connection(engine, owner_id) as conn:
            yield conn


def _update(engine: Engine, run_id: str, owner_id: str | None, values: dict[str, Any]) -> None:
    with _scoped(engine, owner_id) as conn:
        conn.execute(update(runs_t).where(runs_t.c.id == run_id).values(**values))


def insert_run(
    engine: Engine,
    *,
    run_id: str,
    owner_id: str,
    persona_id: str,
    task: str,
    started_at: datetime,
) -> None:
    """INSERT the run row in ``running`` state, before the loop starts.

    Args:
        engine: The RLS engine.
        run_id: The run's durable id.
        owner_id: The tenant the run belongs to.
        persona_id: The persona executing the run — MUST belong to ``owner_id``.
        task: The task text shown in the run viewer.
        started_at: The run's start instant (tz-aware UTC).

    Raises:
        RunPersonaOwnerMismatchError: If ``(persona_id, owner_id)`` violates the
            composite FK — i.e. the persona is not this owner's. Raised loudly rather
            than swallowed: silently skipping the insert is the very failure this
            module exists to prevent, and the insert precedes the model spend, so
            failing here costs nothing and surfaces the broken invariant.
    """
    try:
        with _scoped(engine, owner_id) as conn:
            conn.execute(
                insert(runs_t).values(
                    id=run_id,
                    owner_id=owner_id,
                    persona_id=persona_id,
                    task=task,
                    status="running",
                    started_at=started_at,
                )
            )
    except IntegrityError as exc:
        raise RunPersonaOwnerMismatchError(
            "cannot record a run whose persona does not belong to its owner",
            context={"run_id": run_id, "owner_id": owner_id, "persona_id": persona_id},
        ) from exc


def persist_progress(
    engine: Engine,
    *,
    run_id: str,
    event_log: list[dict[str, object]],
    owner_id: str | None = None,
) -> None:
    """Snapshot the event log to ``runs.steps`` as it grows (crash-viewable, D-08-5)."""
    _update(engine, run_id, owner_id, {"steps": event_log})


def persist_final(engine: Engine, *, run_id: str, run: Run, owner_id: str | None = None) -> None:
    """Write the authoritative terminal record from a finished :class:`Run`.

    The run's own ``status`` is written verbatim: every :class:`RunStatus` value is a
    member of the ``runs_status_check`` vocabulary, so no mapping is invented here.
    """
    _update(
        engine,
        run_id,
        owner_id,
        {
            "status": str(run.status),
            "steps": [s.model_dump(mode="json") for s in run.steps],
            "output": run.output,
            "error": run.error,
            "finished_at": run.finished_at,
        },
    )


def persist_terminal(
    engine: Engine,
    *,
    run_id: str,
    status: str,
    error: str | None,
    finished_at: datetime,
    owner_id: str | None = None,
) -> None:
    """Terminate a run that produced no :class:`Run` object (an early stop).

    ``status`` MUST be one of the ``runs_status_check`` values; the steps already
    snapshotted by :func:`persist_progress` are left in place as the record of how far
    the run got.
    """
    _update(
        engine,
        run_id,
        owner_id,
        {"status": status, "error": error, "finished_at": finished_at},
    )


def persist_error(
    engine: Engine, *, run_id: str, message: str, owner_id: str | None = None
) -> None:
    """Mark a run errored when the execution itself raised (no :class:`Run` returned)."""
    _update(engine, run_id, owner_id, {"status": "error", "error": message})
