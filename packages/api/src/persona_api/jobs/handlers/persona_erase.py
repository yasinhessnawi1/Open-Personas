"""The ``persona_erase`` job: the durable half of erasing a persona (R9-284, D-R9-284-erase).

The route claims the erasure, fences the persona and deletes its row, then enqueues this job,
which runs :func:`persona_api.services.erasure_service.erase_persona` off the event loop. The
ledger row is the source of truth and the job is only its carrier: a retried, reclaimed or
duplicated job finds the erasure finished and does nothing, and an erasure whose job died is
enqueued again at startup under a new key (:func:`resume_persona_erasures`).

The handler works on the platform engine the deps carry, never on the job owner's RLS scope,
because after the row delete ``persona_app`` can no longer see what is left to remove. What
keeps it inside one owner is the ledger: the job may only run an erasure its own owner claimed.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from persona.errors import PermanentJobError
from persona.jobs import LONG_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.stores.postgres import PostgresBackend
from pydantic import field_validator
from sqlalchemy import or_, select

from persona_api.db.audit_factory import build_audit_logger
from persona_api.db.engine import aware_utc
from persona_api.db.models import jobs as jobs_t
from persona_api.errors import ErasureRecordError
from persona_api.jobs.handlers.avatar import JobTypeCarrier
from persona_api.jobs.queue import JobQueue
from persona_api.services.erasure_graph import erase_persona_graph
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import (
    PERSONA_ERASE_JOB_TYPE,
    PersonaEraseDeps,
    erase_persona,
)
from persona_api.workspace_paths import is_persona_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.jobs import JobContext, JobRegistry
    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Connection, Engine

    from persona_api.config import APIConfig
    from persona_api.jobs.queue import JobRecord
    from persona_api.services.erasure_ledger import Erasure
    from persona_api.storage import FileStorage

__all__ = [
    "PERSONA_ERASE_JOB_TYPE",
    "STALL_AFTER",
    "PersonaEraseSweep",
    "build_persona_erase_sweep",
    "stalled_persona_erasures",
    "PersonaEraseHandler",
    "PersonaErasePayload",
    "build_persona_erase_deps",
    "enqueue_persona_erase",
    "persona_erase_idempotency_key",
    "persona_erase_queue_available",
    "register_persona_erase_handler",
    "register_persona_erase_tenant",
    "resume_persona_erasures",
    "resume_token",
]

_log = get_logger("api.erasure")

_ERASURE_ID = re.compile(r"erasure_[0-9a-f]{32}")
_TOKEN = re.compile(r"[0-9]{0,32}")
_MAX_ATTEMPTS = 8


class PersonaErasePayload(JobPayload):
    """Which erasure to run. Ids only: the ledger row holds everything else."""

    erasure_id: str
    persona_id: str
    # Empty for the route's job; a resume carries the ledger's last update so it gets its own
    # key after an earlier job for the same erasure died.
    resume_token: str = ""

    @field_validator("erasure_id")
    @classmethod
    def _an_erasure_id(cls, value: str) -> str:
        if _ERASURE_ID.fullmatch(value) is None:
            msg = "not an erasure id"
            raise ValueError(msg)
        return value

    @field_validator("persona_id")
    @classmethod
    def _a_persona_id(cls, value: str) -> str:
        if not is_persona_id(value):
            msg = "not a persona id"
            raise ValueError(msg)
        return value

    @field_validator("resume_token")
    @classmethod
    def _a_token(cls, value: str) -> str:
        if _TOKEN.fullmatch(value) is None:
            msg = "not a resume token"
            raise ValueError(msg)
        return value


def persona_erase_idempotency_key(payload: PersonaErasePayload) -> str:
    """One job per erasure, and one per resume of it."""
    suffix = f":{payload.resume_token}" if payload.resume_token else ""
    return f"erase:{payload.erasure_id}{suffix}"


def resume_token(erasure: Erasure) -> str:
    """The key part of a resume: the ledger's last change, so a stalled erasure is not re-keyed."""
    return erasure.updated_at.strftime("%Y%m%d%H%M%S%f")


class PersonaEraseHandler:
    """Runs one persona erasure from its ledger row. Idempotent by the ledger."""

    def __init__(self, *, deps: PersonaEraseDeps) -> None:
        self._deps = deps

    async def handle(self, payload: PersonaErasePayload, context: JobContext) -> None:
        erasure = ErasureLedger(self._deps.engine).get(SubjectKind.PERSONA, payload.persona_id)
        if erasure is None:
            # The ledger row is written before the job is enqueued, so this is no erasure at all.
            raise PermanentJobError("no erasure is recorded for this persona")
        if erasure.owner_id != context.owner_id:
            raise PermanentJobError("the erasure belongs to another owner")
        if erasure.id != payload.erasure_id or erasure.state is ErasureState.COMPLETE:
            return
        try:
            await asyncio.to_thread(erase_persona, self._deps, erasure)
        except ErasureRecordError:
            # A refusal (another owner's persona) is the same on every retry.
            raise PermanentJobError("the erasure was refused") from None


def register_persona_erase_handler(registry: JobRegistry, *, deps: PersonaEraseDeps) -> None:
    """Register the erase tenant. ``deps.engine`` must see every tenant's rows."""
    registry.register(
        JobTypeSpec(
            type=PERSONA_ERASE_JOB_TYPE,
            payload_model=PersonaErasePayload,
            handler=PersonaEraseHandler(deps=deps),
            idempotency_key=persona_erase_idempotency_key,
            retry=RetryPolicy(
                max_attempts=_MAX_ATTEMPTS, base_backoff_seconds=10.0, max_backoff_seconds=900.0
            ),
            lease=LONG_LEASE,
        )
    )


def build_persona_erase_deps(
    *,
    platform_engine: Engine,
    embedder: Embedder,
    config: APIConfig,
    file_storage: FileStorage | None,
    workspace_root: Path | None,
) -> PersonaEraseDeps:
    """The erasure's deps over Postgres: memory and store audit on the same platform engine."""
    return PersonaEraseDeps(
        engine=platform_engine,
        memory_backend=PostgresBackend(engine=platform_engine, embedder=embedder),
        audit_logger=build_audit_logger(config, platform_engine),
        file_storage=file_storage,
        workspace_root=workspace_root,
        audit_root=Path(config.audit_root),
    )


def register_persona_erase_tenant(
    registry: JobRegistry,
    *,
    platform_engine: Engine,
    embedder: Embedder,
    config: APIConfig,
    file_storage: FileStorage | None,
    workspace_root: Path | None,
) -> None:
    """Compose the deps on ``platform_engine`` and register the erase handler (worker root)."""
    register_persona_erase_handler(
        registry,
        deps=build_persona_erase_deps(
            platform_engine=platform_engine,
            embedder=embedder,
            config=config,
            file_storage=file_storage,
            workspace_root=workspace_root,
        ),
    )


def enqueue_persona_erase(
    queue: JobQueue, erasure: Erasure, *, token: str = ""
) -> JobRecord | None:
    """Enqueue the job for this erasure; a repeat with the same key is absorbed."""
    payload = PersonaErasePayload(
        erasure_id=erasure.id, persona_id=erasure.subject_id, resume_token=token
    )
    return queue.enqueue(
        type=PERSONA_ERASE_JOB_TYPE,
        owner_id=erasure.owner_id,
        payload=payload.model_dump(),
        idempotency_key=persona_erase_idempotency_key(payload),
        max_attempts=_MAX_ATTEMPTS,
    )


def persona_erase_queue_available(
    *, job_queue: object | None, in_process_worker: object | None
) -> bool:
    """Whether a worker in this process consumes ``persona_erase``; else the erasure runs inline."""
    if job_queue is None or not isinstance(in_process_worker, JobTypeCarrier):
        return False
    return PERSONA_ERASE_JOB_TYPE in in_process_worker.job_types


_LIVE_STATES = ("queued", "claimed", "running")


def _has_live_job(conn: Connection, erasure_id: str) -> bool:
    """Whether a job for this erasure (the route's, or any resume's) is still to run."""
    key = f"erase:{erasure_id}"
    found = conn.execute(
        select(jobs_t.c.id)
        .where(
            jobs_t.c.type == PERSONA_ERASE_JOB_TYPE,
            jobs_t.c.state.in_(_LIVE_STATES),
            or_(
                jobs_t.c.idempotency_key == key,
                jobs_t.c.idempotency_key.startswith(f"{key}:", autoescape=True),
            ),
        )
        .limit(1)
    ).first()
    return found is not None


def resume_persona_erasures(*, engine: Engine, run: Callable[[Erasure, str], object]) -> int:
    """Hand every unfinished persona erasure to ``run`` with its resume token; return how many.

    Called at startup and from the periodic sweep, on an engine that sees every tenant. ``run``
    enqueues the job, or runs the erasure inline where no worker consumes it. An erasure that
    already has a job waiting or running is left to it.
    """
    pending = [
        e for e in ErasureLedger(engine).unfinished() if e.subject_kind is SubjectKind.PERSONA
    ]
    with engine.connect() as conn:
        orphaned = [e for e in pending if not _has_live_job(conn, e.id)]
    for erasure in orphaned:
        run(erasure, resume_token(erasure))
    return len(orphaned)


#: An erasure unfinished for longer than this is stalled: the operator is told.
STALL_AFTER = timedelta(minutes=15)

#: How long after an erasure completes the sweep runs its graph step once more (review H2):
#: the longest a job that was running at completion can keep its lease.
GRAPH_RECHECK_AFTER = timedelta(seconds=LONG_LEASE.lease_seconds)


def stalled_persona_erasures(engine: Engine, *, now: datetime) -> list[Erasure]:
    """Unfinished persona erasures that failed, or that have been running too long."""
    stalled = []
    for erasure in ErasureLedger(engine).unfinished():
        if erasure.subject_kind is not SubjectKind.PERSONA:
            continue
        requested = aware_utc(erasure.requested_at)
        too_old = requested is not None and now - requested >= STALL_AFTER
        if erasure.state is ErasureState.FAILED or too_old:
            stalled.append(erasure)
    return stalled


class PersonaEraseSweep:
    """The periodic half of resuming (review M2): resume what has no live job, report stalls.

    Each stalled erasure is reported once per process, as an ERROR log line starting
    ``OPERATOR ALERT``: the api cannot reach the host's alert marker, so the host's own
    ``bin/erasurecheck.sh`` timer is what raises the kit's alert (deploy/server).
    """

    def __init__(self, *, engine: Engine, run: Callable[[Erasure, str], object]) -> None:
        self._engine = engine
        self._run = run
        self._reported: set[str] = set()
        self._graph_rechecked: set[str] = set()

    def __call__(self) -> int:
        resumed = resume_persona_erasures(engine=self._engine, run=self._run)
        self._recheck_the_graph(datetime.now(UTC))
        for erasure in stalled_persona_erasures(self._engine, now=datetime.now(UTC)):
            if erasure.id in self._reported:
                continue
            self._reported.add(erasure.id)
            _log.error(
                "OPERATOR ALERT: persona erasure stalled erasure={eid} state={state} error={code}",
                eid=erasure.id,
                state=erasure.state.value,
                code=erasure.error_code or "none",
            )
        return resumed

    def _recheck_the_graph(self, now: datetime) -> None:
        """Run the graph step once more for each recently finished erasure (review H2).

        The backstop behind the synthesizer's own check: a job that was already past every
        check when the erasure finished can still write. Waited until a long lease has passed
        since completion, so any job that was running then has finished or lost its lease; the
        window closes later, so a sweep that missed a few cycles still gets to it. Once per
        erasure per process. Counts above zero are logged, never recorded: the erasure is
        complete.
        """
        settled = now - GRAPH_RECHECK_AFTER
        for erasure in ErasureLedger(self._engine).completed_since(now - 3 * GRAPH_RECHECK_AFTER):
            done = aware_utc(erasure.completed_at)
            if erasure.subject_kind is not SubjectKind.PERSONA or done is None or done > settled:
                continue
            if erasure.id in self._graph_rechecked:
                continue
            self._graph_rechecked.add(erasure.id)
            counts = erase_persona_graph(
                self._engine, owner_id=erasure.owner_id, persona_id=erasure.subject_id
            )
            if any(counts.values()):
                _log.warning(
                    "late graph writes removed after an erasure erasure={eid} counts={counts}",
                    eid=erasure.id,
                    counts=counts,
                )


def build_persona_erase_sweep(platform_engine: Engine) -> PersonaEraseSweep:
    """The worker's sweep: resumes go to the queue on the platform engine."""
    queue = JobQueue(platform_engine)
    return PersonaEraseSweep(
        engine=platform_engine,
        run=lambda erasure, token: enqueue_persona_erase(queue, erasure, token=token),
    )
