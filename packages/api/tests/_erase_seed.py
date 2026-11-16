"""A persona with something in every place an erasure has to reach, and a sibling beside it.

Shared by the erase tests. Runs on the community engine (SQLite) with a Chroma memory store and
local file storage: the legacy community edition exactly, and the same SQL the Postgres editions
run for every table here. ``memory_chunks``, the guard trigger and the graph are Postgres only and
are pinned in the integration tests.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import psutil
from persona.audit import MemoryAuditLogger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.chroma import ChromaBackend
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.db.models import metadata
from persona_api.storage import LocalFileStorage
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, insert, select

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

    from persona.stores.backend import Backend
    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Connection, Engine

OWNER = "user_alice"
OTHER_OWNER = "user_bob"
PERSONA = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SIBLING = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
STRANGER = "persona_cccccccccccccccccccccccccccccccc"
SECRET = "the words the user said"

# Memory kinds keyed by the persona; documents are keyed by the conversation.
PERSONA_KINDS = ("identity", "self_facts", "worldview", "episodic", "episodic_gist", "core_memory")
_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Seeded:
    engine: Engine
    memory: Backend
    audit: MemoryAuditLogger
    storage: LocalFileStorage
    workspace_root: Path
    audit_root: Path


def _filler(column: Any) -> Any:  # noqa: ANN401 - a SQLAlchemy column
    kind = column.type
    if isinstance(kind, Boolean):
        return False
    if isinstance(kind, (Integer, Float)):
        return 0
    if isinstance(kind, DateTime):
        return _NOW
    if isinstance(kind, JSON) or column.name in {"payload", "params", "metadata", "candidate"}:
        return {}
    return "x"


def row(conn: Connection, table: str, **values: Any) -> None:  # noqa: ANN401
    """Insert one row, filling every other required column with a harmless value."""
    t = metadata.tables[table]
    full = dict(values)
    for column in t.columns:
        needs = not column.nullable and column.server_default is None and column.default is None
        if column.name not in full and needs and column.autoincrement is not True:
            full[column.name] = _filler(column)
    conn.execute(insert(t).values(**full))


def chunk(chunk_id: str) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=f"{SECRET} {chunk_id}",
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.USER, logical_id=f"logical-{chunk_id}", version=1, written_at=_NOW
        ),
    )


def conversation_of(persona_id: str) -> str:
    return f"conv{persona_id[-12:]}"


def _persona_rows(conn: Connection, owner: str, pid: str) -> None:
    conv = conversation_of(pid)
    task = f"task_{pid[-6:]}"
    run = f"run_{pid[-6:]}"
    schedule = f"sched_{pid[-6:]}"
    row(conn, "personas", id=pid, owner_id=owner, yaml="x: 1")
    row(conn, "conversations", id=conv, owner_id=owner, persona_id=pid, title="t", origin="chat")
    row(conn, "messages", id=f"msg_{pid[-6:]}", conversation_id=conv, role="user", content=SECRET)
    row(
        conn,
        "schedules",
        id=schedule,
        owner_id=owner,
        target_job_type="task_leg",
        one_time_at=_NOW,
        missed_fire_policy="fire-late-once",
        payload_template={"task_id": task, "subject": SECRET},
    )
    row(
        conn,
        "schedules",
        id=f"initsched:{pid}",
        owner_id=owner,
        target_job_type="initiative",
        one_time_at=_NOW,
        missed_fire_policy="fire-late-once",
    )
    row(
        conn,
        "tasks",
        id=task,
        owner_id=owner,
        persona_id=pid,
        contract_json={"subject": SECRET},
        kind="standing",
        state="active",
        conversation_id=conv,
        run_ids=[run],
        schedule_id=schedule,
    )
    row(
        conn, "runs", id=run, owner_id=owner, persona_id=pid, task="t", status="completed", steps=[]
    )
    for job_id, table, state in (
        (f"job_{pid[-6:]}", "jobs", "queued"),
        (f"arch_{pid[-6:]}", "jobs_archive", "succeeded"),
    ):
        row(
            conn,
            table,
            id=job_id,
            type="delegated_turn",
            owner_id=owner,
            payload={"conversation_id": conv, "verbatim_ask": SECRET},
            idempotency_key=job_id,
            state=state,
        )
    row(
        conn,
        "notifications",
        id=f"note_{pid[-6:]}",
        owner_id=owner,
        kind="persona_ready",
        ref_id=pid,
        level="info",
        message_key="k",
        params={},
    )
    row(
        conn,
        "initiative_notices",
        id=f"notice_{pid[-6:]}",
        owner_id=owner,
        persona_id=pid,
        opportunity_key=f"o_{pid[-6:]}",
        trigger="t",
        source="s",
        disposition="d",
        candidate={"text": SECRET},
    )
    row(
        conn,
        "initiative_declines",
        id=f"decl_{pid[-6:]}",
        owner_id=owner,
        persona_id=pid,
        opportunity_key=f"o_{pid[-6:]}",
        trigger="t",
        source="s",
    )
    row(
        conn,
        "schedule_tombstones",
        id=f"tomb_{pid[-6:]}",
        owner_id=owner,
        schedule_id="s",
        persona_id=pid,
        target_job_type="t",
        action="deleted",
        reason="r",
    )
    row(
        conn,
        "tool_audit_events",
        id=f"tool_{pid[-6:]}",
        persona_id=pid,
        tool_name="code",
        action="run",
        resource="/x",
        is_error=False,
        metadata={"code": SECRET},
    )
    row(
        conn,
        "store_audit_events",
        id=f"store_{pid[-6:]}",
        persona_id=pid,
        action="write",
        store="episodic",
        source="user",
        reason=SECRET,
        chunk_ids=["c"],
        logical_ids=["l"],
        metadata={"to_version": "2"},
    )
    # The graph's events ride the owner's id and name the persona as written_by (CSA-1).
    row(
        conn,
        "store_audit_events",
        id=f"graphaudit_{pid[-6:]}",
        persona_id=owner,
        action="write",
        store="knowledge_graph",
        source="persona_self",
        written_by=pid,
        reason=SECRET,
        chunk_ids=["node"],
        logical_ids=[],
        metadata={"action": "created"},
    )
    # Another owner's row naming this persona's conversation: never theirs to scrub.
    row(
        conn,
        "audit_log",
        id=f"foreign_{pid[-6:]}",
        user_id=OTHER_OWNER if owner != OTHER_OWNER else OWNER,
        action="upload.create",
        target=f"{conv}/doc_x",
        metadata={"workspace_path": f"x/{SECRET}.pdf", "verbatim_ask": SECRET},
    )
    row(
        conn,
        "audit_log",
        id=f"audit_{pid[-6:]}",
        user_id=owner,
        action="delegated_turn.execute",
        target=conv,
        metadata={"persona_id": pid, "verbatim_ask": SECRET, "outcome": "done"},
    )
    # File names are content (review L3): an upload and a document upload.
    row(
        conn,
        "audit_log",
        id=f"upload_{pid[-6:]}",
        user_id=owner,
        action="upload.create",
        target=pid,
        metadata={"workspace_path": f"uploads/{SECRET}.png", "media_type": "image/png"},
    )
    row(
        conn,
        "audit_log",
        id=f"docup_{pid[-6:]}",
        user_id=owner,
        action="upload.create",
        target=f"{conv}/doc_{SECRET}",
        metadata={"workspace_path": f"conversations/{conv}/{SECRET}.pdf", "format": "pdf"},
    )
    trigger = f"trig_{pid[-6:]}"
    row(
        conn,
        "event_triggers",
        id=trigger,
        owner_id=owner,
        persona_id=pid,
        event_kind="message",
        filter={},
        action={},
    )
    for kind, ref in (
        ("event_trigger_dropped", trigger),
        ("run_terminal", run),
        ("schedule_fired", schedule),
    ):
        row(
            conn,
            "notifications",
            id=f"{kind}_{pid[-6:]}",
            owner_id=owner,
            kind=kind,
            ref_id=ref,
            level="info",
            message_key="k",
            params={},
        )


def _persona_memory(memory: Backend, pid: str) -> None:
    for kind in PERSONA_KINDS:
        memory.upsert(persona_id=pid, store_kind=kind, chunks=[chunk(f"{pid[-6:]}-{kind}")])
    memory.upsert(
        persona_id=conversation_of(pid), store_kind="document", chunks=[chunk(f"{pid[-6:]}-doc")]
    )


def _persona_files(seeded: Seeded, owner: str, pid: str) -> None:
    seeded.storage.put(f"{owner}/{pid}/uploads/a.png", SECRET.encode())
    local_only = seeded.workspace_root / owner / pid / "sandbox"
    local_only.mkdir(parents=True, exist_ok=True)
    (local_only / "out.txt").write_text(SECRET, encoding="utf-8")
    (seeded.audit_root / f"{pid}.jsonl").write_text(
        json.dumps(
            {
                "persona_id": pid,
                "chunk_ids": ["c"],
                "reason": SECRET,
                # R9-291: a content key in the kept log's metadata, beside an id-like one.
                "metadata": {"verbatim_ask": SECRET, "to_version": "2"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with (seeded.audit_root / f"{owner}.jsonl").open("a", encoding="utf-8") as graph_log:
        graph_log.write(
            json.dumps(
                {"persona_id": owner, "written_by": pid, "chunk_ids": ["n"], "reason": SECRET}
            )
            + "\n"
        )
    (seeded.audit_root / f"{pid}.tools.jsonl").write_text(json.dumps({"code": SECRET}), "utf-8")


def seed_into(engine: Engine, memory: Backend, tmp_path: Path) -> Seeded:
    """Seed the three personas into ``engine`` and ``memory``, with files under ``tmp_path``."""
    workspace_root = tmp_path / "workspace"
    audit_root = tmp_path / "audit"
    workspace_root.mkdir()
    audit_root.mkdir()
    seeded = Seeded(
        engine=engine,
        memory=memory,
        audit=MemoryAuditLogger(),
        storage=LocalFileStorage(workspace_root),
        workspace_root=workspace_root,
        audit_root=audit_root,
    )
    with engine.begin() as conn:
        row(conn, "users", id=OWNER, email="alice@x.test")
        row(conn, "users", id=OTHER_OWNER, email="bob@x.test")
        _persona_rows(conn, OWNER, PERSONA)
        _persona_rows(conn, OWNER, SIBLING)
        _persona_rows(conn, OTHER_OWNER, STRANGER)
    for owner, pid in ((OWNER, PERSONA), (OWNER, SIBLING), (OTHER_OWNER, STRANGER)):
        _persona_memory(seeded.memory, pid)
        _persona_files(seeded, owner, pid)
    return seeded


def open_files_under(root: Path) -> list[str]:
    """Files under ``root`` this process still holds open (a leak on Windows, R9-284).

    psutil lists the process's open files, but on the GitHub Windows runner the listing itself
    is refused (``AccessDenied`` from a handle under ``C:\\$Extend``). Then the guard falls back to
    :func:`files_held_open_under`, which finds the same leak another way; it never just passes.
    """
    try:
        listed = psutil.Process().open_files()
    except (psutil.AccessDenied, PermissionError):
        return files_held_open_under(root)
    prefix = str(root.resolve()).lower()
    return sorted(f.path for f in listed if f.path.lower().startswith(prefix))


_PROBE = ".leakprobe"


def files_held_open_under(root: Path) -> list[str]:
    """Files under ``root`` that cannot be renamed: on Windows, the ones a handle holds open.

    Python, SQLite and Chroma's index files open without ``FILE_SHARE_DELETE``, so Windows
    refuses to rename a file while any of them holds it, and allows it once it is closed. Each
    file is renamed and renamed back. Only meaningful on Windows (POSIX renames open files), which
    is the only place psutil's listing has been refused.
    """
    held: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.suffix != _PROBE):
        probe = path.with_name(path.name + _PROBE)
        try:
            path.rename(probe)
        except PermissionError:
            held.append(str(path))
            continue
        probe.rename(path)
    return held


@contextmanager
def legacy_edition(tmp_path: Path, embedder: Embedder) -> Iterator[Seeded]:
    """A seeded legacy edition that gives back every file it opened.

    Chroma's persistent client keeps each collection's segment files open until it is closed,
    and a SQLite engine keeps its pooled connection. Never closed, a few dozen tests of them
    reached Windows' 512 open-file limit and failed unrelated tests that ran later. The exit
    asserts nothing under ``tmp_path`` is still open.
    """
    seeded = seed(tmp_path, embedder)
    try:
        yield seeded
    finally:
        assert isinstance(seeded.memory, ChromaBackend)
        seeded.memory.close()
        seeded.engine.dispose()
        leaked = open_files_under(tmp_path)
        assert leaked == [], f"the erase seed left files open: {leaked[:5]}"


def seed(tmp_path: Path, embedder: Embedder) -> Seeded:
    """The legacy community edition: SQLite and Chroma. Prefer :func:`legacy_edition`."""
    engine = make_community_engine(tmp_path / "community.db")
    create_community_schema(engine)
    memory = ChromaBackend(persist_path=tmp_path / "chroma", embedder=embedder)
    return seed_into(engine, memory, tmp_path)


def ids_naming(engine: Engine, table: str, value: str) -> list[str]:
    """Rows of ``table`` where any column holds ``value`` or JSON that contains it."""
    t = metadata.tables[table]
    with engine.connect() as conn:
        rows = conn.execute(select(t)).mappings().all()
    return sorted(
        str(r["id"]) if "id" in r else str(r)
        for r in rows
        if any(
            value in (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for v in r.values()
        )
    )


# --- what the erasure does with every place a persona can be named (review L5) ---------------
#
# Derived from the live schema by the tests (the SQLite inspector here, information_schema on
# Postgres), so a new table with a persona column, or a new JSON column, fails until it is
# given a line below: a decision, not an accident.

PERSONA_TABLES: dict[str, str] = {
    "personas": "the row step",
    "memory_chunks": "the memory step, through the store doors",
    "initiative_notices": "the rows step",
    "tool_audit_events": "the rows step (the tool audit goes, by the ruling)",
    "initiative_declines": "kept, persona nulled: the owner's intent",
    "schedule_tombstones": "kept, persona nulled: the owner's intent",
    "store_audit_events": "kept by the ruling: ids and labels",
    "erasures": "the ledger itself",
    # Gone by the personas foreign key cascade at the row step.
    "approval_proposals": "cascade",
    "cadence_counters": "cascade",
    "calls": "cascade",
    "connector_channels": "pointer cleared by delete_persona_row",
    "connector_conversations": "cascade",
    "conversations": "cascade",
    "deferred_digest": "cascade",
    "event_triggers": "cascade, ids captured for their notifications",
    "persona_mcp_assignments": "cascade",
    "runs": "cascade",
    "skill_consents": "cascade",
    "suspended_personas": "cascade",
    "tasks": "cascade",
}

JSON_COLUMNS: dict[str, str] = {
    # Searched for the persona's ids.
    "jobs.payload": "fence and rows steps: jobs naming the persona go",
    "jobs_archive.payload": "rows step: archived jobs naming the persona go",
    "schedules.payload_template": "fence step: schedules naming the persona go",
    "audit_log.metadata": "rows step: content keys removed, rows kept",
    "graph_nodes.provenance": "graph step",
    "graph_node_versions.provenance": "graph step",
    "graph_edges.provenance": "graph step",
    "graph_entities.provenance": "graph step",
    # The row goes, with whatever its JSON holds.
    "graph_nodes.metadata": "graph step deletes the row, or keeps the user's node",
    "graph_node_versions.metadata": "graph step deletes the row, or keeps the user's node",
    "graph_entities.aliases": "graph step deletes the entity, or keeps a shared one",
    "memory_chunks.metadata": "memory step",
    "initiative_notices.candidate": "rows step",
    "notifications.params": "rows step, by ref_id",
    "tool_audit_events.metadata": "rows step",
    "messages.tool_calls": "cascade",
    "messages.channel": "cascade",
    "messages.images": "cascade",
    "messages.stream_events": "cascade",
    "tasks.contract_json": "cascade",
    "tasks.run_ids": "cascade",
    "runs.steps": "cascade",
    "task_checkpoints.checkpoint_json": "cascade",
    "approval_proposals.arguments_json": "cascade",
    "approval_proposals.categories_json": "cascade",
    "approval_decisions.edited_arguments_json": "cascade",
    "event_triggers.filter": "cascade",
    "event_triggers.action": "cascade",
    # Kept: ids, labels and counts, or not about a persona.
    "store_audit_events.chunk_ids": "kept by the ruling",
    "store_audit_events.logical_ids": "kept by the ruling",
    "store_audit_events.metadata": "kept by the ruling",
    "schedule_tombstones.reason": "kept: the owner's intent",
    "erasures.captured": "the ledger itself",
    "erasures.counts": "the ledger itself",
    "user_mcp_servers.discovered_tools": "the user's, not a persona's",
}


def uncovered(persona_tables: Iterable[str], json_columns: Iterable[str]) -> list[str]:
    """Every table with a persona column and every JSON column the erasure has no line for."""
    return sorted(
        {*(t for t in persona_tables if t not in PERSONA_TABLES)}
        | {c for c in json_columns if c not in JSON_COLUMNS}
    )
