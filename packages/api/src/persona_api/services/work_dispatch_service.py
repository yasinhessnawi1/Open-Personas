"""One door for "just run this": an ad hoc task and its first leg (Spec W1, T2; D-W1-1).

A bare dispatch used to be a separate execution: an in-process asyncio task the run
registry drove, with no contract, no checkpoint, no continuation, no category gate, and a
``runs`` row that belonged to nothing (R9-100). Under D-W1-1 a run is an execution of a
task, so a one-off creates an **ad hoc task** (``TaskKind.AD_HOC``, D-W1-2) with a degenerate
contract (the brief is the goal, the default category policy, no authoring call, D-W1-7),
starts it, and enqueues its first leg on the worker: the same leg path every standing task
runs, so the one-off gets checkpoints, continuation, dead-work handling, and the consent
envelope by construction.

What this deliberately does NOT do: reserve the interactive long-op slot (the worker's
per-user claim cap and the leg box are the bounds, D-W1-24); open the ``runs`` row (the leg
handler opens it when the worker claims the job, through the one runs writer); or accept a
brief over :data:`MAX_BRIEF_CHARS` (the goal is recited at the head of every leg and counts
against the checkpoint budget; the refusal is a human sentence, D-W1-28).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.errors import PersonaNotFoundError
from persona.logging import get_logger
from persona.tasks import Contract, Task, TaskKind, UserDispatch
from sqlalchemy import select

from persona_api.db.models import personas as personas_t
from persona_api.errors import WorkBriefTooLongError
from persona_api.tasks.handler import enqueue_task_leg
from persona_api.tasks.store import TaskStore

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from persona_api.jobs.queue import JobQueue

__all__ = [
    "BRIEF_TOO_LONG_MESSAGE",
    "MAX_BRIEF_CHARS",
    "PERSONA_GONE_MESSAGE",
    "AdHocDispatch",
    "dispatch_ad_hoc",
    "dispatch_task",
]

_log = get_logger("api.services.work_dispatch")

#: The cap on a one-off brief (D-W1-7). The goal leads every leg's reconstruction and counts
#: against the 2000-token checkpoint budget, so an unbounded brief would starve the leg.
MAX_BRIEF_CHARS = 8_000

#: The refusal, as a sentence a person reads (D-W1-28): never a raw validation body.
BRIEF_TOO_LONG_MESSAGE = (
    "That brief is longer than I can take in one go. "
    "Keep it under 8,000 characters and I'll get started."
)


@dataclass(frozen=True)
class AdHocDispatch:
    """The confirmation a dispatch returns: the task that now carries the work."""

    task_id: str
    persona_id: str
    kind: str
    state: str


#: The refusal when the persona a task names no longer exists (a retry after a deletion).
PERSONA_GONE_MESSAGE = "That persona is gone, so this cannot run again."


def _require_persona(rls_engine: Engine, persona_id: str, *, message: str | None = None) -> None:
    """404 unless the persona is the caller's (RLS makes another tenant's invisible)."""
    with rls_engine.begin() as conn:
        found = conn.execute(select(personas_t.c.id).where(personas_t.c.id == persona_id)).first()
    if found is None:
        raise PersonaNotFoundError(message or "persona not found", context={"id": persona_id})


def dispatch_task(
    rls_engine: Engine,
    queue: JobQueue,
    task: Task,
    *,
    now: datetime,
    persona_gone_message: str | None = None,
) -> Task:
    """The ONE dispatch composition: verify the persona, create, start, enqueue the first leg.

    Shared by the one-off door (:func:`dispatch_ad_hoc`) and by a retry of a finished task
    (``task_control_service.retry_task``), so the two cannot drift (the R9-081 shape). The
    persona check runs first, before any write: a persona deleted since the task was made is
    refused with a sentence, never a leg that fails at runtime.

    Returns:
        The started task (``ACTIVE``), its first leg queued with a :class:`UserDispatch`.

    Raises:
        PersonaNotFoundError: The task's persona is not the caller's (or no longer exists).
    """
    _require_persona(rls_engine, task.persona_id, message=persona_gone_message)
    store = TaskStore(rls_engine)
    store.create(task)
    started = store.start(task.owner_id, task.id, now=now)
    enqueue_task_leg(
        queue,
        owner_id=task.owner_id,
        task_id=task.id,
        predecessor_seq=None,
        trigger=UserDispatch(dispatched_at=now),
    )
    return started


def dispatch_ad_hoc(
    *,
    rls_engine: Engine,
    queue: JobQueue,
    owner_id: str,
    persona_id: str,
    brief: str,
    now: datetime | None = None,
) -> AdHocDispatch:
    """Create an ad hoc task for ``brief``, start it, and enqueue its first leg.

    Args:
        rls_engine: The owner-scoped engine (the request's RLS engine).
        queue: The job queue the first leg is enqueued on (owner-scoped enqueue).
        owner_id: The caller.
        persona_id: The persona that will do the work; must be the caller's.
        brief: The one-off ask, verbatim; becomes the contract goal.
        now: Injected clock (tests); defaults to the current UTC instant.

    Returns:
        The :class:`AdHocDispatch` confirmation (a command returning its ids, CQS).

    Raises:
        WorkBriefTooLongError: The brief exceeds :data:`MAX_BRIEF_CHARS`.
        PersonaNotFoundError: The persona is not the caller's.
    """
    text = brief.strip()
    if len(text) > MAX_BRIEF_CHARS:
        raise WorkBriefTooLongError(
            BRIEF_TOO_LONG_MESSAGE,
            context={"max_chars": str(MAX_BRIEF_CHARS), "chars": str(len(text))},
        )
    at = now if now is not None else datetime.now(UTC)
    task_id = f"task_{uuid.uuid4().hex}"
    started = dispatch_task(
        rls_engine,
        queue,
        Task(
            id=task_id,
            owner_id=owner_id,
            persona_id=persona_id,
            contract=Contract(goal=text),
            kind=TaskKind.AD_HOC,
            created_at=at,
            updated_at=at,
        ),
        now=at,
    )
    _log.info("ad hoc task dispatched", task_id=task_id, persona_id=persona_id)
    return AdHocDispatch(
        task_id=task_id,
        persona_id=persona_id,
        kind=started.kind.value,
        state=started.state.value,
    )
