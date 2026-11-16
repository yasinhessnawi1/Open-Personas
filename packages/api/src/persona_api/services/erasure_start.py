"""Starting a persona erasure from the route, and running it where no worker will (R9-284).

The route's half of D-R9-284-erase, in this order:

1. Capture, on the caller's RLS engine: the persona must be the caller's (a 404 otherwise), and
   the ids the job needs are read while their rows exist.
2. Claim the erasure in the ledger, on the platform engine (the ledger is SELECT-only to
   ``persona_app``).
3. Fence: delete the persona's schedules and jobs so nothing fires for it.
4. Delete the row in one transaction on the RLS engine. The persona is gone from the owner's
   view now, and no memory or graph write can name it (the memory owner guard, and the graph
   write guard that holds the persona's row in each write).

Then the route hands the erasure to the ``persona_erase`` job, or, where this process runs no
worker that consumes it (legacy SQLite, a keyless boot), to :func:`run_persona_erasure_inline`
in the background. A failure there is recorded in the ledger and the erasure resumes at the
next start (:func:`resume_persona_erasures_on_startup`).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from persona.errors import PersonaNotFoundError
from persona.logging import get_logger

from persona_api.errors import ErasureRecordError
from persona_api.jobs.handlers.persona_erase import (
    RESUME_GRACE,
    PersonaEraseSweep,
    build_persona_erase_deps,
    enqueue_persona_erase,
    persona_erase_queue_available,
)
from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind
from persona_api.services.erasure_lock import erasure_lock
from persona_api.services.erasure_service import (
    ErasureRaceError,
    PersonaEraseDeps,
    capture_persona,
    delete_row_capturing,
    erase_persona,
    fence_persona,
)
from persona_api.services.legacy_audit_sweep import run_legacy_sweep_logged
from persona_api.workspace_paths import is_persona_id

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from persona_api.services.erasure_ledger import Erasure

__all__ = [
    "begin_persona_erasure",
    "dispatch_persona_erasure",
    "erase_persona_now",
    "erase_deps_from_state",
    "resume_persona_erasures_on_startup",
    "run_persona_erasure_inline",
]

_log = get_logger("api.erasure")


def begin_persona_erasure(
    *, rls_engine: Engine, platform_engine: Engine, owner_id: str, persona_id: str
) -> Erasure:
    """Capture, claim, fence and delete the row. Raises ``PersonaNotFoundError`` if not yours."""
    not_found = PersonaNotFoundError("persona not found", context={"id": persona_id[:80]})
    if not is_persona_id(persona_id):
        raise not_found
    try:
        with rls_engine.connect() as conn:
            captured = capture_persona(conn, owner_id=owner_id, persona_id=persona_id)
    except ErasureRecordError:
        raise not_found from None
    ledger = ErasureLedger(platform_engine)
    erasure = ledger.claim(
        owner_id=owner_id,
        subject_kind=SubjectKind.PERSONA,
        subject_id=persona_id,
        captured=captured,
    )
    # Held while this request fences and deletes, so a sweep cannot run the erasure under it
    # (review L4). If another run holds it, that run fences and deletes the row itself.
    with erasure_lock(platform_engine, erasure.id) as held:
        if not held:
            return erasure
        ledger.record_step(erasure.id, "fence", fence_persona(platform_engine, erasure))
        try:
            deleted = delete_row_capturing(rls_engine, erasure, ledger_engine=platform_engine)
        except ErasureRaceError:
            # Conversations kept appearing under the row lock. The erasure is claimed and
            # fenced; its job deletes the row and retries this race (review LOW): accepted,
            # not a 500.
            _log.warning("persona row delete raced; left to the job erasure={eid}", eid=erasure.id)
            return erasure
        if deleted == 0:
            raise not_found
        ledger.record_step(erasure.id, "row", {"personas": 1})
    return erasure


def erase_deps_from_state(state: Any) -> PersonaEraseDeps | None:  # noqa: ANN401 - app.state
    """The erasure's deps for this process, or ``None`` when it has no platform engine.

    On Postgres the memory transport and the store audit are rebuilt on the platform engine,
    because the request path's are on ``persona_app``, which cannot see what is left once the
    row is gone. The legacy edition uses its own Chroma store and audit.
    """
    engine = getattr(state, "platform_engine", None)
    if engine is None:
        return None
    file_storage = getattr(state, "file_storage", None)
    workspace_root = getattr(state, "workspace_root", None)
    if engine.dialect.name == "postgresql":
        return build_persona_erase_deps(
            platform_engine=engine,
            embedder=state.embedder,
            config=state.config,
            file_storage=file_storage,
            workspace_root=workspace_root,
        )
    memory = getattr(state, "memory_backend", None)
    audit = getattr(state, "audit_logger", None)
    if memory is None or audit is None:
        return None
    return PersonaEraseDeps(
        engine=engine,
        memory_backend=memory,
        audit_logger=audit,
        file_storage=file_storage,
        workspace_root=workspace_root,
        audit_root=getattr(state, "audit_root", None),
    )


def run_persona_erasure_inline(deps: PersonaEraseDeps, erasure: Erasure) -> None:
    """Run the erasure here and now. A failure is logged; the ledger keeps it for a resume."""
    try:
        erase_persona(deps, erasure)
    except Exception as exc:  # noqa: BLE001 - recorded in the ledger by erase_persona; resumed
        _log.error(
            "inline persona erasure failed; it resumes at the next start erasure={eid} error={err}",
            eid=erasure.id,
            err=type(exc).__name__,
        )


def erase_persona_now(
    state: Any,  # noqa: ANN401 - app.state
    *,
    rls_engine: Engine,
    owner_id: str,
    persona_id: str,
) -> None:
    """Erase a persona here and now, through the persona erasure, before returning (R9-293).

    For a create that failed after its row committed: the half-made persona is claimed,
    fenced, deleted and erased in full (memory, graph, files, rows) before the request answers.
    A step that fails raises; the ledger keeps the erasure, and the startup resume finishes it.
    """
    deps = erase_deps_from_state(state)
    platform = getattr(state, "platform_engine", None)
    if deps is None or platform is None:
        msg = "no platform engine to erase the persona on"
        raise RuntimeError(msg)
    erasure = begin_persona_erasure(
        rls_engine=rls_engine, platform_engine=platform, owner_id=owner_id, persona_id=persona_id
    )
    erase_persona(deps, erasure)


def dispatch_persona_erasure(state: Any, erasure: Erasure, *, token: str = "") -> bool:  # noqa: ANN401
    """Enqueue the erasure's job if a worker here consumes it. False: run it inline instead."""
    queue = getattr(state, "job_queue", None)
    worker = getattr(state, "in_process_worker", None)
    if not persona_erase_queue_available(job_queue=queue, in_process_worker=worker):
        return False
    enqueue_persona_erase(queue, erasure, token=token)  # type: ignore[arg-type]
    return True


#: How often a process with no worker for the erase job sweeps for unfinished erasures.
INLINE_SWEEP_SECONDS = 300.0


def resume_persona_erasures_on_startup(
    state: Any,  # noqa: ANN401 - app.state
    *,
    interval_seconds: float = INLINE_SWEEP_SECONDS,
    grace: timedelta = RESUME_GRACE,
) -> asyncio.Task[None] | None:
    """Resume unfinished persona erasures now, and keep sweeping where no worker will.

    Each sweep hands an erasure with no live job to the queue, or runs it inline in a thread,
    and reports a stalled one (:class:`PersonaEraseSweep`). Where a worker here consumes the
    erase job, its maintenance cadence sweeps from then on and this runs once; otherwise
    (legacy SQLite, a worker-less boot) it repeats every ``interval_seconds`` until the task
    is cancelled at shutdown. A failed sweep is logged and never stops the process.

    Before the first sweep it runs the legacy audit sweep once (R9-290), so a persona deleted
    before the ledger existed is claimed and erased by that first sweep.

    Returns the background task (held by the caller) or ``None`` when there is nothing to run
    on.
    """
    deps = erase_deps_from_state(state)
    if deps is None:
        return None

    def _run(erasure: Erasure, token: str) -> None:
        if not dispatch_persona_erasure(state, erasure, token=token):
            run_persona_erasure_inline(deps, erasure)

    sweep = PersonaEraseSweep(engine=deps.engine, run=_run, grace=grace)

    def _once() -> None:
        try:
            count = sweep()
        except Exception as exc:  # noqa: BLE001 - must not stop the process; logged loudly
            _log.error("could not resume unfinished erasures error={err}", err=type(exc).__name__)
            return
        if count:
            _log.warning("resumed unfinished persona erasures count={n}", n=count)

    def _worker_sweeps() -> bool:
        return persona_erase_queue_available(
            job_queue=getattr(state, "job_queue", None),
            in_process_worker=getattr(state, "in_process_worker", None),
        )

    def _writable() -> bool | None:
        writable: bool | None = getattr(state, "erasure_ledger_writable", None)
        return writable

    async def _loop() -> None:
        # R9-290: personas deleted before the ledger left audit files; claim their erasures
        # (or clean the files in place) once, before the resume picks them up. Only where the
        # ledger is known writable (review H1 a): a boot that fell back to a row-level-security
        # engine would see no persona and mistake every one for a leftover. A probe that had
        # no answer at boot (None) is asked again on every pass until the sweep has run once
        # (round 2 LOW-6): /healthz re-probes and sets it.
        swept = False
        first = True
        while True:
            if not swept and _writable() is True:
                await asyncio.to_thread(run_legacy_sweep_logged, deps.engine, deps.audit_root)
                swept = True
            worker = _worker_sweeps()
            if first or not worker:  # a worker here resumes on its own cadence after the first
                await asyncio.to_thread(_once)
            first = False
            if worker and (swept or _writable() is False):
                return
            await asyncio.sleep(interval_seconds)

    return asyncio.get_running_loop().create_task(_loop())
