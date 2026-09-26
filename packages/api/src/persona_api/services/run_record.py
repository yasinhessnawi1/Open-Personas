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
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from persona_runtime.agentic.run import RunStatus
from persona_runtime.agentic.step import PersonaOriginatedNote, StepType
from sqlalchemy import insert, update
from sqlalchemy.exc import IntegrityError

from persona_api.db.engine import rls_connection
from persona_api.db.models import runs as runs_t
from persona_api.errors import RunPersonaOwnerMismatchError, RunRecordEmptyError
from persona_api.middleware.rls_context import current_user_id
from persona_api.services.user_facing_errors import (
    owner_on_free_plan,
    user_facing_error_message_for,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from persona_runtime.agentic.run import Run
    from sqlalchemy import Connection, Engine

__all__ = [
    "RunStopReason",
    "insert_run",
    "persist_error",
    "persist_final",
    "persist_origination",
    "persist_progress",
    "persist_terminal",
]


class RunStopReason(StrEnum):
    """Why a run was stopped before it ended on its own (R9-158); ``runs.stop_reason``.

    The one vocabulary behind ``runs_stop_reason_check`` (a unit test holds the two
    equal). A run that ends on its own records none. The box bounds and the drain carry
    the same values as :class:`persona.tasks.LegBoxLimit`, because the executor hands over
    the reason its cancel token was tripped with.
    """

    PAUSED = "paused"
    CANCELLED = "cancelled"
    BUDGET = "budget"
    WALL_CLOCK = "wall_clock"
    STEPS = "steps"
    DRAIN = "drain"
    APPROVAL = "approval"


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
    task_id: str | None = None,
) -> None:
    """INSERT the run row in ``running`` state, before the loop starts.

    Args:
        engine: The RLS engine.
        run_id: The run's durable id.
        owner_id: The tenant the run belongs to.
        persona_id: The persona executing the run — MUST belong to ``owner_id``.
        task: The task text shown in the run viewer.
        started_at: The run's start instant (tz-aware UTC).
        task_id: The task this run executes (Spec W1, D-W1-1). ``None`` only for the
            legacy in-process path, which retires with T2; a leg always passes it.

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
                    task_id=task_id,
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


def persist_final(
    engine: Engine,
    *,
    run_id: str,
    run: Run,
    owner_id: str | None = None,
    stop_reason: RunStopReason | None = None,
) -> None:
    """Write the authoritative terminal record from a finished :class:`Run`.

    The run's own ``status`` is written verbatim: every :class:`RunStatus` value is a
    member of the ``runs_status_check`` vocabulary, so no mapping is invented here.

    This REPLACES the event-log snapshot :func:`persist_progress` left behind, so what a
    reopened run can ever show is exactly what :class:`~persona_runtime.agentic.step.Step`
    carries. A run event that is never reduced onto a step does not survive this write
    (R9-157: that is how the guard disclosure came to exist only while a run was watched).

    A failed run is rewritten for its reader on the way in (R9-097): ``runs.error`` is
    shown on the run page, and a capacity exhaustion stringifies to our provider names,
    model ids and routing strategy. The loop hands the failure over honestly and this
    picks the sentence a person sees, in both places the record states it.

    ``stop_reason`` says why a CANCELLED run was stopped (R9-158). It is written only on a
    cancelled run: any other status already says how the run ended, and a reason beside
    it would contradict the row.
    """
    error, steps = _reader_safe_failure(engine, run, owner_id)
    reason = stop_reason.value if stop_reason is not None else None
    _update(
        engine,
        run_id,
        owner_id,
        {
            "status": str(run.status),
            "steps": steps,
            "output": run.output,
            "error": error,
            "stop_reason": reason if run.status is RunStatus.CANCELLED else None,
            "finished_at": run.finished_at,
        },
    )


def _reader_safe_failure(
    engine: Engine, run: Run, owner_id: str | None
) -> tuple[str | None, list[dict[str, Any]]]:
    """``(error, steps)`` as the record should state them, for a person (R9-097).

    Returns the run's own text and steps untouched unless the failure is one the message
    catalogue rewrites. When it is, the sentence replaces BOTH the row's ``error`` and the
    closing ``ERROR`` step's text, because the run page reads the first and the step
    timeline reads the second, and two different accounts of one failure is worse than
    either alone. The plan lookup only happens on that path, so a healthy run pays nothing.
    """
    steps = [s.model_dump(mode="json") for s in run.steps]
    if run.status is not RunStatus.ERROR or run.error_class is None:
        return run.error, steps
    # The ambient scope is the run worker's own bind for the run's lifetime; a caller
    # that passed an owner explicitly wins over it.
    owner = owner_id or current_user_id.get() or ""
    safe = user_facing_error_message_for(
        run.error_class, on_free_plan=owner_on_free_plan(engine, owner)
    )

    if safe is None:
        return run.error, steps
    if steps and steps[-1].get("type") == StepType.ERROR.value:
        steps[-1] = {**steps[-1], "content": safe}
    return safe, steps


def persist_origination(
    engine: Engine,
    *,
    run_id: str,
    run: Run,
    conversation_id: str,
    owner_id: str | None = None,
) -> None:
    """Stamp the terminal record with the message the run's persona sent on its own.

    Within-runtime origination (Spec C0) fires only AFTER :func:`persist_final`, and the
    ``persona_originated`` event it pushes never enters the event log, so the durable
    record used to end in silence while the live stream showed the persona speaking. This
    rewrites ``steps`` with a :class:`PersonaOriginatedNote` appended to the LAST step's
    ``notes`` (the channel R9-157 introduced for exactly this: a run event whose story must
    outlive the run). Nothing else on the row changes, and the notification the terminal
    write already sent is not repeated.

    Raises:
        RunRecordEmptyError: ``run`` has no steps to carry the note. A completed run always
            has its final step, so this is a caller bug, not a runtime condition.
    """
    if not run.steps:
        raise RunRecordEmptyError(
            "cannot stamp origination on a run with no steps",
            context={"run_id": run_id, "conversation_id": conversation_id},
        )
    note = PersonaOriginatedNote(conversation_id=conversation_id)
    last = run.steps[-1]
    stamped = last.model_copy(update={"notes": [*last.notes, note]})
    steps = [*run.steps[:-1], stamped]
    _update(
        engine,
        run_id,
        owner_id,
        {"steps": [s.model_dump(mode="json") for s in steps]},
    )


def persist_terminal(
    engine: Engine,
    *,
    run_id: str,
    status: str,
    error: str | None,
    finished_at: datetime,
    owner_id: str | None = None,
    stop_reason: RunStopReason | None = None,
) -> None:
    """Terminate a run that produced no :class:`Run` object (an early stop).

    ``status`` MUST be one of the ``runs_status_check`` values; the steps already
    snapshotted by :func:`persist_progress` are left in place as the record of how far
    the run got. ``stop_reason`` says why it stopped (R9-158), e.g. the approval gate.
    """
    _update(
        engine,
        run_id,
        owner_id,
        {
            "status": status,
            "error": error,
            "stop_reason": stop_reason.value if stop_reason is not None else None,
            "finished_at": finished_at,
        },
    )


def persist_error(
    engine: Engine, *, run_id: str, message: str, owner_id: str | None = None
) -> None:
    """Mark a run errored when the execution itself raised (no :class:`Run` returned)."""
    _update(engine, run_id, owner_id, {"status": "error", "error": message})
