"""Starting a persona erasure from the route, and running it where no worker will (R9-284).

The route's half of D-R9-284-erase, in this order:

1. Capture, on the caller's RLS engine: the persona must be the caller's (a 404 otherwise), and
   the ids the job needs are read while their rows exist.
2. Claim the erasure in the ledger, on the platform engine (the ledger is SELECT-only to
   ``persona_app``).
3. Fence: delete the persona's schedules and jobs so nothing fires for it.
4. Delete the row in one transaction on the RLS engine. The persona is gone from the owner's
   view now, and no memory can be written for it.

Then the route hands the erasure to the ``persona_erase`` job, or, where this process runs no
worker that consumes it (legacy SQLite, a keyless boot), to :func:`run_persona_erasure_inline`
in the background. A failure there is recorded in the ledger and the erasure resumes at the
next start (:func:`resume_persona_erasures_on_startup`).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from persona.errors import PersonaNotFoundError
from persona.logging import get_logger

from persona_api.errors import ErasureRecordError
from persona_api.jobs.handlers.persona_erase import (
    build_persona_erase_deps,
    enqueue_persona_erase,
    persona_erase_queue_available,
    resume_persona_erasures,
)
from persona_api.services import persona_service
from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind
from persona_api.services.erasure_service import (
    PersonaEraseDeps,
    capture_persona,
    erase_persona,
    fence_persona,
)
from persona_api.workspace_paths import is_persona_id

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from persona_api.services.erasure_ledger import Erasure

__all__ = [
    "begin_persona_erasure",
    "dispatch_persona_erasure",
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
    ledger.record_step(erasure.id, "fence", fence_persona(platform_engine, erasure))
    persona_service.delete_persona(rls_engine=rls_engine, persona_id=persona_id, owner_id=owner_id)
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


def dispatch_persona_erasure(state: Any, erasure: Erasure, *, token: str = "") -> bool:  # noqa: ANN401
    """Enqueue the erasure's job if a worker here consumes it. False: run it inline instead."""
    queue = getattr(state, "job_queue", None)
    worker = getattr(state, "in_process_worker", None)
    if not persona_erase_queue_available(job_queue=queue, in_process_worker=worker):
        return False
    enqueue_persona_erase(queue, erasure, token=token)  # type: ignore[arg-type]
    return True


def resume_persona_erasures_on_startup(state: Any) -> asyncio.Task[None] | None:  # noqa: ANN401
    """Hand every unfinished persona erasure back to the job, or run them inline in a thread.

    Returns the background task (held by the caller) or ``None`` when there is nothing to run
    on. A failure to read the ledger is logged and never stops the process from starting.
    """
    deps = erase_deps_from_state(state)
    if deps is None:
        return None

    def _resume() -> None:
        def _run(erasure: Erasure, token: str) -> None:
            if not dispatch_persona_erasure(state, erasure, token=token):
                run_persona_erasure_inline(deps, erasure)

        try:
            count = resume_persona_erasures(engine=deps.engine, run=_run)
        except Exception as exc:  # noqa: BLE001 - startup must not fail on this; logged loudly
            _log.error("could not resume unfinished erasures error={err}", err=type(exc).__name__)
            return
        if count:
            _log.warning("resumed unfinished persona erasures count={n}", n=count)

    return asyncio.get_running_loop().create_task(asyncio.to_thread(_resume))
