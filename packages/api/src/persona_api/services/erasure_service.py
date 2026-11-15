"""Erasing a persona: every step, in order, resumable and counted (R9-284, D-R9-284-erase).

The route claims the erasure in the ledger with the ids it captured, fences off what would run
for the persona, and deletes its row, so the persona is gone from the owner's view at once and
nothing new can be written for it (row level security on hosted, the memory owner guard on both
Postgres editions). Everything else is the job's: :func:`erase_persona` runs :data:`STEPS` in
order and records each step's counts.

Every step is idempotent and owner scoped, so a resumed erasure simply runs them all again: a
step whose work is done adds 0 to its counts (the ledger adds, it does not overwrite). A step
that fails marks the erasure failed with a code, never the exception's message (those carry
paths), and re-raises so the queue retries.

The graph is the owner's: what only the persona contributed goes, and the persona's entries
leave the rest (:mod:`persona_api.services.erasure_graph`).

What stays, by the ruling: the store audit (ids and labels), the owner's declines and schedule
tombstones (their persona reference is cleared), and ``audit_log`` rows with their content keys
removed. What goes: memory in K8-D-14 order (evidence before what was derived from it, the core
block last), stored and local files, the tool audit and its ``{persona}.tools.jsonl``, and the
schedules, jobs, notifications and notices that name the persona.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from persona.logging import get_logger
from persona.stores import EpisodicStore, IdentityStore, SelfFactsStore, WorldviewStore
from persona.stores.core_memory import CORE_MEMORY_KIND, CoreMemoryStore
from persona.stores.document_store import DocumentStore
from persona.tools._sandbox import require_path_segment
from sqlalchemy import delete, select, update

from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import initiative_declines as initiative_declines_t
from persona_api.db.models import initiative_notices as initiative_notices_t
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import jobs_archive as jobs_archive_t
from persona_api.db.models import notifications as notifications_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.db.models import schedule_tombstones as schedule_tombstones_t
from persona_api.db.models import schedules as schedules_t
from persona_api.db.models import tasks as tasks_t
from persona_api.db.models import tool_audit_events as tool_audit_events_t
from persona_api.errors import ErasureRecordError
from persona_api.services import audit_service
from persona_api.services.erasure_graph import erase_persona_graph
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.persona_service import delete_persona_row, remove_persona_folder

if TYPE_CHECKING:
    from pathlib import Path

    from persona.audit import AuditLogger
    from persona.stores.backend import Backend
    from sqlalchemy import Table
    from sqlalchemy.engine import Connection, Engine

    from persona_api.services.erasure_ledger import Erasure
    from persona_api.storage import FileStorage

__all__ = [
    "AUDIT_CONTENT_KEYS",
    "PERSONA_ERASE_JOB_TYPE",
    "STEPS",
    "PersonaEraseDeps",
    "capture_persona",
    "erase_persona",
    "fence_persona",
]

_log = get_logger("api.erasure")

PERSONA_ERASE_JOB_TYPE = "persona_erase"

# ``audit_log`` metadata keys that hold what a person said rather than ids and outcomes.
AUDIT_CONTENT_KEYS = frozenset({"verbatim_ask"})


@dataclass(frozen=True)
class PersonaEraseDeps:
    """What an erasure works through.

    ``engine`` sees every row it has to remove: the BYPASSRLS platform engine on hosted (the
    persona's rows are invisible to ``persona_app`` once its row is gone), the community engine
    otherwise. ``memory_backend`` is a transport over that same database (or Chroma, legacy).
    """

    engine: Engine
    memory_backend: Backend
    audit_logger: AuditLogger
    file_storage: FileStorage | None
    workspace_root: Path | None
    audit_root: Path | None


Step = Callable[[PersonaEraseDeps, "Erasure"], dict[str, int]]


def _refuse(reason: str) -> ErasureRecordError:
    return ErasureRecordError("the erasure was refused", context={"reason": reason})


def _names(value: Any, ids: frozenset[str]) -> bool:  # noqa: ANN401 - stored JSON
    """Whether a stored JSON value names any of ``ids`` anywhere inside it."""
    if isinstance(value, str):
        return value in ids
    if isinstance(value, Mapping):
        return any(_names(v, ids) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_names(v, ids) for v in value)
    return False


def _subject_ids(erasure: Erasure) -> frozenset[str]:
    """The persona and every id captured for it: what a stored row may name."""
    captured = (i for ids in erasure.captured.values() for i in ids)
    return frozenset({erasure.subject_id, *captured})


def _column(conn: Connection, stmt: Any) -> list[str]:  # noqa: ANN401 - a select
    return sorted({str(value) for (value,) in conn.execute(stmt) if value is not None})


def capture_persona(conn: Connection, *, owner_id: str, persona_id: str) -> dict[str, list[str]]:
    """The ids later steps need, read while their rows still exist.

    Refuses a persona that is not this owner's. Schedules are the persona's initiative scan,
    the schedules behind its tasks, and any of the owner's schedules whose template names the
    persona, its tasks or its conversations.
    """
    owned = conn.execute(
        select(personas_t.c.id).where(
            personas_t.c.id == persona_id, personas_t.c.owner_id == owner_id
        )
    ).first()
    if owned is None:
        raise _refuse("not_the_owners_persona")
    conversations = _column(
        conn,
        select(conversations_t.c.id).where(
            conversations_t.c.persona_id == persona_id, conversations_t.c.owner_id == owner_id
        ),
    )
    task_rows = conn.execute(
        select(tasks_t.c.id, tasks_t.c.schedule_id, tasks_t.c.run_ids).where(
            tasks_t.c.persona_id == persona_id, tasks_t.c.owner_id == owner_id
        )
    ).all()
    tasks = sorted(str(t.id) for t in task_rows)
    runs = _column(
        conn,
        select(runs_t.c.id).where(runs_t.c.persona_id == persona_id, runs_t.c.owner_id == owner_id),
    )
    runs = sorted({*runs, *(str(r) for t in task_rows for r in (t.run_ids or []))})
    named = frozenset({persona_id, *conversations, *tasks})
    schedules = {str(t.schedule_id) for t in task_rows if t.schedule_id is not None}
    for schedule_id, template in conn.execute(
        select(schedules_t.c.id, schedules_t.c.payload_template).where(
            schedules_t.c.owner_id == owner_id
        )
    ):
        if schedule_id == f"initsched:{persona_id}" or _names(template, named):
            schedules.add(str(schedule_id))
    return {
        "conversations": conversations,
        "tasks": tasks,
        "runs": runs,
        "schedules": sorted(schedules),
    }


def _delete_naming(conn: Connection, table: Table, erasure: Erasure, column: str) -> int:
    """Delete the owner's rows of ``table`` whose JSON ``column`` names the persona's ids.

    The erase job's own rows are never matched: they name the persona too.
    """
    ids = _subject_ids(erasure)
    rows = conn.execute(
        select(table.c.id, table.c[column]).where(
            table.c.owner_id == erasure.owner_id, table.c.type != PERSONA_ERASE_JOB_TYPE
        )
    ).all()
    doomed = [r.id for r in rows if _names(r[1], ids)]
    if doomed:
        conn.execute(delete(table).where(table.c.id.in_(doomed)))
    return len(doomed)


def fence_persona(engine: Engine, erasure: Erasure) -> dict[str, int]:
    """Stop what would run for the persona: its schedules and its queued or running jobs.

    The route calls this before the row goes; the job runs it again first and last, for
    anything enqueued in between. Owner scoped and idempotent.
    """
    ids = _subject_ids(erasure)
    scan = f"initsched:{erasure.subject_id}"
    with engine.begin() as conn:
        rows = conn.execute(
            select(schedules_t.c.id, schedules_t.c.payload_template).where(
                schedules_t.c.owner_id == erasure.owner_id
            )
        ).all()
        captured = set(erasure.captured.get("schedules", []))
        doomed = [
            r.id
            for r in rows
            if r.id in captured or r.id == scan or _names(r.payload_template, ids)
        ]
        if doomed:
            conn.execute(delete(schedules_t).where(schedules_t.c.id.in_(doomed)))
        jobs = _delete_naming(conn, jobs_t, erasure, "payload")
    return {"schedules": len(doomed), "jobs": jobs}


def _require_not_anothers(engine: Engine, erasure: Erasure) -> None:
    """Refuse an erasure whose persona exists under a different owner.

    The memory, document and tool audit steps are keyed by persona id alone, so this is what
    keeps a ledger row from reaching a persona that is not its owner's.
    """
    with engine.connect() as conn:
        owner = conn.execute(
            select(personas_t.c.owner_id).where(personas_t.c.id == erasure.subject_id)
        ).scalar()
    if owner is not None and owner != erasure.owner_id:
        raise _refuse("not_the_owners_persona")


def _step_fence(deps: PersonaEraseDeps, erasure: Erasure) -> dict[str, int]:
    return fence_persona(deps.engine, erasure)


def _step_row(deps: PersonaEraseDeps, erasure: Erasure) -> dict[str, int]:
    with deps.engine.begin() as conn:
        removed = delete_persona_row(conn, owner_id=erasure.owner_id, persona_id=erasure.subject_id)
    return {"personas": removed}


def _step_memory(deps: PersonaEraseDeps, erasure: Erasure) -> dict[str, int]:
    """Every memory kind through its own door: evidence first, the core block last."""
    backend, audit = deps.memory_backend, deps.audit_logger
    pid = erasure.subject_id
    counts = {
        store.STORE_KIND: store(backend=backend, audit_logger=audit).delete(pid)
        for store in (IdentityStore, SelfFactsStore, WorldviewStore)
    }
    documents = DocumentStore(backend=backend)
    counts["document"] = sum(documents.delete(c) for c in erasure.captured.get("conversations", []))
    counts.update(EpisodicStore(backend=backend, audit_logger=audit).delete_by_kind(pid))
    # The episodic door retires the core block's chain; any core row outside that chain (one
    # written before chains, or under another logical id) goes through the kind's own door.
    core = CoreMemoryStore(backend=backend, audit_logger=audit)
    counts[CORE_MEMORY_KIND] = counts.get(CORE_MEMORY_KIND, 0) + core.delete(pid)
    return counts


def _remove_tool_log(audit_root: Path | None, persona_id: str) -> int:
    if audit_root is None:
        return 0
    log = audit_root / f"{require_path_segment(persona_id, what='persona id')}.tools.jsonl"
    if not log.exists():
        return 0
    log.unlink()
    return 1


def _step_files(deps: PersonaEraseDeps, erasure: Erasure) -> dict[str, int]:
    """Stored objects, the local folder for writers that bypass storage, and the tool log."""
    owner, pid = erasure.owner_id, erasure.subject_id
    objects = deps.file_storage.delete_prefix(f"{owner}/{pid}") if deps.file_storage else 0
    folder = (
        remove_persona_folder(deps.workspace_root, owner_id=owner, persona_id=pid)
        if deps.workspace_root is not None
        else False
    )
    return {
        "objects": objects,
        "folder": int(folder),
        "tool_log": _remove_tool_log(deps.audit_root, pid),
    }


def _scrub_audit_log(conn: Connection, erasure: Erasure) -> int:
    """Remove content keys from the owner's audit rows about the persona; keep the rows."""
    rows = conn.execute(
        select(audit_log_t.c.id, audit_log_t.c.metadata).where(
            audit_log_t.c.user_id == erasure.owner_id,
            audit_log_t.c.target.in_(sorted(_subject_ids(erasure))),
        )
    ).all()
    scrubbed = 0
    for audit_id, meta in rows:
        if not isinstance(meta, Mapping) or not AUDIT_CONTENT_KEYS & set(meta):
            continue
        kept = {k: v for k, v in meta.items() if k not in AUDIT_CONTENT_KEYS}
        conn.execute(update(audit_log_t).where(audit_log_t.c.id == audit_id).values(metadata=kept))
        scrubbed += 1
    return scrubbed


def _step_rows(deps: PersonaEraseDeps, erasure: Erasure) -> dict[str, int]:
    """The rows with no foreign key to the persona, which its row's cascade cannot reach."""
    owner, pid = erasure.owner_id, erasure.subject_id
    refs = sorted(_subject_ids(erasure))
    with deps.engine.begin() as conn:
        counts = {
            "jobs_archive": _delete_naming(conn, jobs_archive_t, erasure, "payload"),
            "notifications": conn.execute(
                delete(notifications_t).where(
                    notifications_t.c.owner_id == owner, notifications_t.c.ref_id.in_(refs)
                )
            ).rowcount,
            "initiative_notices": conn.execute(
                delete(initiative_notices_t).where(
                    initiative_notices_t.c.owner_id == owner,
                    initiative_notices_t.c.persona_id == pid,
                )
            ).rowcount,
            "tool_audit_events": conn.execute(
                delete(tool_audit_events_t).where(tool_audit_events_t.c.persona_id == pid)
            ).rowcount,
            "audit_log_scrubbed": _scrub_audit_log(conn, erasure),
        }
        for name, table in (
            ("initiative_declines", initiative_declines_t),
            ("schedule_tombstones", schedule_tombstones_t),
        ):
            counts[name] = conn.execute(
                update(table)
                .where(table.c.owner_id == owner, table.c.persona_id == pid)
                .values(persona_id=None)
            ).rowcount
    return counts


def _step_graph(deps: PersonaEraseDeps, erasure: Erasure) -> dict[str, int]:
    """What only the persona contributed to the owner's graph, and its entries in the rest."""
    return erase_persona_graph(
        deps.engine, owner_id=erasure.owner_id, persona_id=erasure.subject_id
    )


def _step_late_writes(deps: PersonaEraseDeps, erasure: Erasure) -> dict[str, int]:
    """Anything written or enqueued while the steps above ran. Expected to be all zeros."""
    return {
        **_step_memory(deps, erasure),
        **_step_graph(deps, erasure),
        **fence_persona(deps.engine, erasure),
    }


STEPS: tuple[tuple[str, Step], ...] = (
    ("fence", _step_fence),
    ("row", _step_row),
    ("memory", _step_memory),
    ("graph", _step_graph),
    ("files", _step_files),
    ("rows", _step_rows),
    ("late_writes", _step_late_writes),
)


def _flatten(counts: Mapping[str, Mapping[str, int]]) -> dict[str, str]:
    return {f"{step}_{name}": str(n) for step, named in counts.items() for name, n in named.items()}


def _run_steps(
    deps: PersonaEraseDeps,
    ledger: ErasureLedger,
    erasure: Erasure,
    steps: Iterable[tuple[str, Step]],
) -> None:
    for name, step in steps:
        try:
            counts = step(deps, erasure)
        except Exception as exc:
            _log.warning(
                "persona erasure step failed erasure={eid} step={step} error={err}",
                eid=erasure.id,
                step=name,
                err=type(exc).__name__,
            )
            ledger.fail(erasure.id, f"{name}_failed")
            raise
        ledger.record_step(erasure.id, name, counts)


def erase_persona(deps: PersonaEraseDeps, erasure: Erasure) -> None:
    """Run every step for this erasure, record its counts, complete it and audit it.

    A no-op for an erasure that has finished. Raises what a step raised, after marking the
    erasure failed, so the caller (the queue, or the inline runner) can retry it.
    """
    if erasure.subject_kind is not SubjectKind.PERSONA:
        raise _refuse("not_a_persona_erasure")
    ledger = ErasureLedger(deps.engine)
    current = ledger.get(SubjectKind.PERSONA, erasure.subject_id)
    if current is None or current.id != erasure.id or current.state is ErasureState.COMPLETE:
        return
    try:
        _require_not_anothers(deps.engine, current)
    except ErasureRecordError:
        ledger.fail(current.id, "not_the_owners_persona")
        raise
    _run_steps(deps, ledger, current, STEPS)
    ledger.complete(current.id)
    done = ledger.get(SubjectKind.PERSONA, current.subject_id)
    audit_service.record(
        engine=deps.engine,
        user_id=current.owner_id,
        action="persona.erase",
        target=current.subject_id,
        metadata=_flatten(done.counts if done is not None else {}),
    )
