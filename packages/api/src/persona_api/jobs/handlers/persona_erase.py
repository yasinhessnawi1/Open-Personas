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
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from persona.errors import PermanentJobError, PersonaNotFoundError
from persona.jobs import LONG_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.stores.postgres import PostgresBackend
from pydantic import field_validator
from sqlalchemy import func, or_, select

from persona_api.db.audit_factory import build_audit_logger
from persona_api.db.engine import aware_utc
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import personas as personas_t
from persona_api.errors import ErasureRecordError
from persona_api.jobs.handlers.avatar import JobTypeCarrier
from persona_api.jobs.queue import JobQueue
from persona_api.services.erasure_graph import (
    GraphMaintenanceBusyError,
    erase_persona_graph,
    erased_personas_in_graph,
)
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import (
    PERMANENT_REFUSALS,
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
    "RESUME_GRACE",
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


#: An erasure changed more recently than this is left alone by a resume (review L4): the
#: route that claimed it may still be fencing it, deleting its row or handing it off.
RESUME_GRACE = timedelta(minutes=2)
#: A create whose identity is still not indexed after this never finished (R9-293): its
#: process died between the commit and the undo, and the sweep erases it.
ABANDONED_CREATE_GRACE = timedelta(minutes=15)


def resume_persona_erasures(
    *,
    engine: Engine,
    run: Callable[[Erasure, str], object],
    grace: timedelta = RESUME_GRACE,
) -> int:
    """Hand every unfinished persona erasure to ``run`` with its resume token; return how many.

    Called at startup and from the periodic sweep, on an engine that sees every tenant. ``run``
    enqueues the job, or runs the erasure inline where no worker consumes it. An erasure that
    already has a job waiting or running is left to it, and so is one changed within ``grace``.
    """
    settled = datetime.now(UTC) - grace
    pending = [
        e
        for e in ErasureLedger(engine).unfinished()
        if e.subject_kind is SubjectKind.PERSONA and (aware_utc(e.updated_at) or settled) <= settled
    ]
    with engine.connect() as conn:
        orphaned = [
            e
            for e in pending
            # A permanent refusal would be re-enqueued every sweep forever; the stall alert
            # reports it instead (review L2).
            if e.error_code not in PERMANENT_REFUSALS and not _has_live_job(conn, e.id)
        ]
    for erasure in orphaned:
        run(erasure, resume_token(erasure))
    return len(orphaned)


#: An erasure unfinished for longer than this is stalled: the operator is told.
STALL_AFTER = timedelta(minutes=15)

#: After this many sweeps in a row found the owner's graph lock busy, the backstop waits for
#: it, in a thread, for at most WAIT_LOCK_TIMEOUT_MS (round 7 LOW-4).
DEFERRALS_BEFORE_WAIT = 10
WAIT_LOCK_TIMEOUT_MS = 60_000
#: A blocked owner's operator alert repeats at most this often (round 8 LOW-3).
BACKSTOP_ALERT_EVERY = timedelta(hours=1)

#: The graph backstop's every-sweep look covers erasures completed this recently ...
BACKSTOP_RECENT = timedelta(hours=24)
#: ... and it looks at every completed erasure this often (review MEDIUM-3).
BACKSTOP_FULL_EVERY = timedelta(hours=24)


def _utcnow() -> datetime:
    return datetime.now(UTC)


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

    The graph backstop's full pass is timed in this object, not in a row (review MEDIUM-3).
    The pass is idempotent and only a backstop to the write guard, so a full pass more than
    once a day costs a query, never correctness: a process start runs one (the first sweep),
    and there is one sweep per worker process. A row would hold the day across restarts, at
    the price of a migration and a write per pass for a value nothing else reads.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        run: Callable[[Erasure, str], object],
        grace: timedelta = RESUME_GRACE,
        find_leftovers: Callable[..., list[tuple[str, str]]] = erased_personas_in_graph,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._engine = engine
        self._run = run
        self._grace = grace
        self._reported: set[str] = set()
        self._find_leftovers = find_leftovers
        self._clock = clock
        self._last_full_pass: datetime | None = None
        self._retry: set[tuple[str, str]] = set()
        # Consecutive busy-lock deferrals per (owner, persona), and the waits in flight. A wait
        # runs in a worker thread, so these are shared with it under a lock (round 7 LOW-4).
        self._deferrals: dict[tuple[str, str], int] = {}
        self._waiting: set[str] = set()  # owners with a wait in flight
        self._alerted: dict[str, datetime] = {}  # owner: when it was last alerted
        self._state = threading.Lock()
        self._tasks: set[asyncio.Future[None]] = set()
        self._executor: ThreadPoolExecutor | None = None
        self._finished: set[tuple[str, str]] = set()  # done during the current sweep

    def __call__(self) -> int:
        self._claim_abandoned_creates()
        resumed = resume_persona_erasures(engine=self._engine, run=self._run, grace=self._grace)
        self._graph_backstop()
        for erasure in stalled_persona_erasures(self._engine, now=self._clock()):
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

    def _claim_abandoned_creates(self) -> None:
        """Claim the erasure of every create that never finished (R9-293 review HIGH-2).

        A row with ``create_complete`` false older than :data:`ABANDONED_CREATE_GRACE` is a
        create whose process died between its commit and its undo. Its erasure is claimed and
        its row deleted under the row's lock, still incomplete (``claim_abandoned_create``,
        review MEDIUM-2), and the resume runs the rest. A younger one may still be finishing,
        and is left alone. On Postgres the cutoff is the database's clock (review MEDIUM-2:
        ``created_at`` is the database's ``now()``); the legacy SQLite edition uses this one.
        """
        from persona_api.services.erasure_start import claim_abandoned_create  # noqa: PLC0415

        cutoff: Any = (
            func.now() - ABANDONED_CREATE_GRACE
            if self._engine.dialect.name == "postgresql"
            else self._clock() - ABANDONED_CREATE_GRACE
        )
        try:
            with self._engine.connect() as conn:
                abandoned = conn.execute(
                    select(personas_t.c.id, personas_t.c.owner_id).where(
                        personas_t.c.create_complete.is_(False),
                        personas_t.c.created_at < cutoff,
                    )
                ).all()
        except Exception as exc:  # noqa: BLE001 - logged; the sweep goes on to the resume
            _log.error("abandoned creates could not be read error={err}", err=type(exc).__name__)
            return
        for persona_id, owner_id in abandoned:
            try:
                claim_abandoned_create(
                    self._engine, owner_id=str(owner_id), persona_id=str(persona_id)
                )
            except PersonaNotFoundError:
                # Gone meanwhile (its own undo, or another sweep): nothing is owed (review LOW-4).
                _log.debug("abandoned persona create already gone persona={pid}", pid=persona_id)
            except Exception as exc:  # noqa: BLE001 - per persona: the next is still tried
                _log.error(
                    "OPERATOR ALERT: an abandoned persona create could not be claimed "
                    "persona={pid} error={err}",
                    pid=persona_id,
                    err=type(exc).__name__,
                )

    def _graph_backstop(self) -> None:
        """Take out of the graph any erased persona something still names (review H-2).

        Recurring and idempotent: each sweep asks the graph which erasures completed in the
        last day it still names (nodes, versions, edges, entities and their aliases), and once
        a day which of all completed erasures it does; the graph step runs for each (review
        MEDIUM-3). A persona whose step fails is kept and tried again on the next sweep,
        whichever look found it, and one failure is logged and does not stop the others or the
        stall alerts. The write guard is what keeps new writes out; this catches anything
        written before it, or by a path it does not cover.
        """
        now = self._clock()
        full = self._last_full_pass is None or now - self._last_full_pass >= BACKSTOP_FULL_EVERY
        try:
            found = self._find_leftovers(
                self._engine, since=None if full else now - BACKSTOP_RECENT
            )
        except Exception as exc:  # noqa: BLE001 - logged; the sweep goes on to its alerts
            _log.error("graph backstop could not look error={err}", err=type(exc).__name__)
            found = []
        else:
            if full:
                self._last_full_pass = now
        with self._state:
            self._finished.clear()
            candidates = sorted({*found, *self._retry})
            # An owner whose wait is in flight is left to it; a persona found fresh is kept
            # in the retry set, so the wait or the next sweep does it (round 9 LOW-2).
            self._retry.update(k for k in candidates if k[0] in self._waiting)
            todo = [k for k in candidates if k[0] not in self._waiting]
        waited_for: set[str] = set()  # owners whose wait this sweep spawned
        for key in todo:
            owner_id, persona_id = key
            if owner_id in waited_for:
                with self._state:
                    if key not in self._finished:
                        self._retry.add(key)  # not done yet: the wait or the next sweep does it
                continue
            try:
                # Never wait here: the sweep runs on the worker's event loop, and a
                # consolidation run of this owner may hold the graph lock (round 6 M-3).
                counts = erase_persona_graph(
                    self._engine, owner_id=owner_id, persona_id=persona_id, wait=False
                )
            except GraphMaintenanceBusyError:
                if self._deferred(key):
                    waited_for.add(owner_id)
                continue
            except Exception as exc:  # noqa: BLE001 - per persona: the next is still tried
                with self._state:
                    self._retry.add(key)
                _log.error(
                    "graph backstop failed persona={pid} error={err}",
                    pid=persona_id,
                    err=type(exc).__name__,
                )
                continue
            self._done(key, counts)

    def _done(self, key: tuple[str, str], counts: dict[str, int]) -> None:
        with self._state:
            self._retry.discard(key)
            self._deferrals.pop(key, None)
            self._finished.add(key)
        _log.warning(
            "graph backstop removed what an erased persona left persona={pid} counts={counts}",
            pid=key[1],
            counts=counts,
        )

    def _deferred(self, key: tuple[str, str]) -> bool:
        """A busy lock: try again next sweep; after too many in a row, wait for it (LOW-4).

        One wait per owner at a time, covering every persona of that owner left to do
        (round 8 LOW-3).
        """
        owner_id = key[0]
        with self._state:
            self._retry.add(key)
            count = self._deferrals.get(key, 0) + 1
            self._deferrals[key] = count
            escalate = count >= DEFERRALS_BEFORE_WAIT and owner_id not in self._waiting
            if escalate:
                self._waiting.add(owner_id)
        _log.info(
            "graph backstop deferred, consolidation running persona={pid} deferrals={n}",
            pid=key[1],
            n=count,
        )
        if escalate:
            self._spawn(lambda: self._wait_for_owner(owner_id, count))
        return escalate

    def _spawn(self, work: Callable[[], None]) -> None:
        """Run ``work`` on the sweep's own thread when on an event loop; inline otherwise.

        One dedicated thread (round 8 LOW-3), so a wait of up to a minute never takes a
        thread the loop's other ``to_thread`` work needs; it is shut down by :meth:`close`.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # the inline resume loop: already in a worker thread
            work()
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="graph-backstop-wait"
            )
        future = loop.run_in_executor(self._executor, work)
        self._tasks.add(future)
        future.add_done_callback(self._tasks.discard)

    def close(self) -> None:
        """Stop the waiting thread: no new waits run; one in flight ends at its lock timeout."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def _wait_for_owner(self, owner_id: str, deferrals: int) -> None:
        try:
            with self._state:
                todo = sorted(k for k in self._retry if k[0] == owner_id)
            for key in todo:
                try:
                    counts = erase_persona_graph(
                        self._engine,
                        owner_id=owner_id,
                        persona_id=key[1],
                        wait=True,
                        lock_timeout_ms=WAIT_LOCK_TIMEOUT_MS,
                    )
                except Exception as exc:  # noqa: BLE001 - the operator is told; tried again later
                    self._blocked(owner_id, key[1], deferrals, exc)
                    return  # the lock is still held: the rest would wait as long
                with self._state:
                    self._alerted.pop(owner_id, None)
                self._done(key, counts)
        finally:
            with self._state:
                self._waiting.discard(owner_id)

    def _blocked(self, owner_id: str, persona_id: str, deferrals: int, exc: Exception) -> None:
        """Alert once per owner, then at most hourly while it persists (round 8 LOW-3)."""
        now = self._clock()
        with self._state:
            last = self._alerted.get(owner_id)
            due = last is None or now - last >= BACKSTOP_ALERT_EVERY
            if due:
                self._alerted[owner_id] = now
        if due:
            _log.error(
                "OPERATOR ALERT: graph backstop blocked owner={owner} persona={pid} "
                "deferrals={n} error={err}",
                owner=owner_id,
                pid=persona_id,
                n=deferrals,
                err=type(exc).__name__,
            )
        else:
            _log.warning(
                "graph backstop still blocked owner={owner} persona={pid}",
                owner=owner_id,
                pid=persona_id,
            )


def build_persona_erase_sweep(platform_engine: Engine) -> PersonaEraseSweep:
    """The worker's sweep: resumes go to the queue on the platform engine."""
    queue = JobQueue(platform_engine)
    return PersonaEraseSweep(
        engine=platform_engine,
        run=lambda erasure, token: enqueue_persona_erase(queue, erasure, token=token),
    )
