"""Community legacy → managed-Postgres auto-import (Spec K10, T6).

THE GATE is the crash-resume idempotency test (D-K10-11): a REAL mid-import abort
(a raised exception partway through the units — the in-flight unit's transaction
rolls back, later units never run) → a re-run resumes from the in-target journal →
every unit is imported EXACTLY once (no duplicates, no skips), and the legacy
sources are renamed to ``*.migrated-*`` ONLY once the journal shows every unit
complete. Plus the clean-run path, the intra-unit atomicity property, and the
nothing-to-import (fresh install) path.

A real embedded Postgres is provisioned once (module-scoped); each test migrates
its OWN fresh database inside that instance for clean exactly-once assertions.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.community_import import (
    CommunityImporter,
    maybe_run_community_import,
)
from persona_api.db.community_managed import CommunityDbManager, run_migrations
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

pytest.importorskip("pixeltable_pgserver")

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

_OWNER = "local-owner"


class _AbortError(RuntimeError):
    """A deliberate mid-import abort (stands in for a crash/kill)."""


# ---- legacy source builder -------------------------------------------------


def _build_legacy_store(root: Path, embedder: HashEmbedder384) -> tuple[Path, Path, dict[str, int]]:
    """A realistic legacy community install: SQLite relational + Chroma vectors."""
    from persona.stores.chroma import ChromaBackend

    sqlite_path = root / ".persona_community.db"
    chroma_path = root / ".persona_chroma"
    persona_id = "11111111-1111-4111-8111-111111111111"
    convo_id = "22222222-2222-4222-8222-222222222222"

    engine = make_community_engine(sqlite_path)
    create_community_schema(engine)
    ensure_owner(engine, owner_id=_OWNER, email="local@localhost")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: Astrid')"),
            {"p": persona_id, "o": _OWNER},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, compacted_summary) "
                "VALUES (:c, :o, :p, '')"
            ),
            {"c": convo_id, "o": _OWNER, "p": persona_id},
        )
        for role, content in (("user", "hello there"), ("assistant", "hi back")):
            # Explicit id: raw SQL bypasses the community table's client-side UUID
            # default (a Python-side ColumnDefault fires only on Core/ORM inserts).
            conn.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, role, content) "
                    "VALUES (:i, :c, :r, :t)"
                ),
                {"i": str(uuid4()), "c": convo_id, "r": role, "t": content},
            )
    engine.dispose()

    # Chroma vectors: 2 identity + 1 self_facts (persona-keyed) + 1 document (convo-keyed).
    chroma = ChromaBackend(persist_path=chroma_path, embedder=embedder)

    def _chunk(pid: str, kind: str, textval: str) -> PersonaChunk:
        cid = mint_chunk_id(pid, kind)
        now = datetime.now(UTC)
        return PersonaChunk(
            id=cid,
            text=textval,
            created_at=now,
            provenance=ChunkProvenance(
                source=WriteSource.SYSTEM,
                logical_id=cid,
                version=1,
                written_at=now,
                written_by="test",
            ),
        )

    chroma.upsert(
        persona_id=persona_id,
        store_kind="identity",
        chunks=[
            _chunk(persona_id, "identity", "Astrid is a tenancy-law expert"),
            _chunk(persona_id, "identity", "Astrid works in Oslo"),
        ],
    )
    chroma.upsert(
        persona_id=persona_id,
        store_kind="self_facts",
        chunks=[_chunk(persona_id, "self_facts", "I was built to help with leases")],
    )
    chroma.upsert(
        persona_id=convo_id,
        store_kind="document",
        chunks=[_chunk(convo_id, "document", "a lease agreement excerpt")],
    )
    expected = {"identity": 2, "self_facts": 1, "document": 1, "vector_total": 4}
    return sqlite_path, chroma_path, expected


# ---- embedded managed Postgres (module-scoped) + a fresh DB per test --------


@pytest.fixture(scope="module")
def instance() -> Iterator[CommunityDbManager]:
    base = tempfile.mkdtemp(prefix="k10imp", dir="/tmp")
    mgr = CommunityDbManager(external_url=None, base_dir=Path(base))
    try:
        mgr.start()
        yield mgr
    finally:
        mgr.stop()
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture
def target_url(instance: CommunityDbManager) -> Iterator[str]:
    base_url = instance.database_url  # postgresql+psycopg://postgres:@/persona?host=...
    dbname = f"imp_{uuid4().hex[:12]}"
    admin_url = base_url.replace("/persona?", "/postgres?")
    db_url = base_url.replace("/persona?", f"/{dbname}?")
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    prior_env = os.environ.get("DATABASE_URL")
    try:
        run_migrations(db_url)  # the SAME chain as cloud — a fresh target schema
        yield db_url
    finally:
        if prior_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior_env
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
        admin.dispose()


def _table_counts(engine: Engine, names: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    with engine.connect() as conn:
        for name in names:
            out[name] = int(conn.execute(text(f"SELECT count(*) FROM {name}")).scalar_one())
    return out


def _all_community_table_names() -> list[str]:
    from persona_api.db.community import build_community_metadata

    return [t.name for t in build_community_metadata().sorted_tables]


def _memory_chunk_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM memory_chunks")).scalar_one())


def _journal(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text("SELECT unit_id FROM k10_import_journal")).scalars().all())


# ---- THE GATE: real mid-import abort → resume → exactly-once ----------------


class _AbortAfter(CommunityImporter):
    """Aborts (a real raise) after ``abort_after`` units have COMMITTED."""

    def __init__(self, *, abort_after: int, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._abort_after = abort_after
        self._committed = 0

    def _on_unit_committed(self, unit_id: str) -> None:
        self._committed += 1
        if self._committed >= self._abort_after:
            raise _AbortError(f"aborting after {self._committed} committed units at {unit_id}")


def test_crash_resume_imports_every_unit_exactly_once(
    tmp_path: Path, target_url: str, embedder: HashEmbedder384
) -> None:
    sqlite_path, chroma_path, expected = _build_legacy_store(tmp_path, embedder)
    source_engine = make_community_engine(sqlite_path)
    all_tables = _all_community_table_names()
    source_counts = _table_counts(source_engine, all_tables)
    source_engine.dispose()

    target = create_engine(target_url)

    # --- run 1: abort partway (a REAL interruption after 3 committed units) ---
    aborted = _AbortAfter(
        abort_after=3,
        sqlite_path=sqlite_path,
        chroma_path=chroma_path,
        target_engine=target,
        embedder=embedder,
    )
    with pytest.raises(_AbortError):
        aborted.run()

    journal_after_abort = _journal(target)
    assert len(journal_after_abort) == 3  # exactly the 3 committed units, no more
    # The sources are NOT renamed — the journal is incomplete (rollback-safe).
    assert sqlite_path.exists()
    assert chroma_path.exists()

    # --- run 2: resume with a clean importer → completes ---
    resumed = CommunityImporter(
        sqlite_path=sqlite_path,
        chroma_path=chroma_path,
        target_engine=target,
        embedder=embedder,
    )
    report = resumed.run()

    # Exactly-once: EVERY community table's target count equals the source count.
    target_counts = _table_counts(target, all_tables)
    assert target_counts == source_counts, "every table imported exactly once (no dup, no skip)"
    # The 3 pre-committed units are skipped on resume (not re-imported).
    assert report.units_skipped == 3
    # Vectors re-embedded exactly once (idempotent upsert-by-id).
    assert _memory_chunk_count(target) == expected["vector_total"]
    # The journal is complete → sources renamed to the rollback marker ONLY now.
    assert report.journal_complete is True
    assert report.renamed, "sources renamed once the journal was complete"
    assert not sqlite_path.exists()  # original moved aside
    assert not chroma_path.exists()
    assert any(p.name.startswith(".persona_community.db.migrated-") for p in tmp_path.iterdir())

    # --- run 3: re-run after completion is a clean no-op (fully idempotent) ---
    third = maybe_run_community_import(
        sqlite_path=sqlite_path,
        chroma_path=chroma_path,
        target_engine=target,
        embedder=embedder,
    )
    assert third is None  # sources already migrated away → nothing to import
    assert _table_counts(target, all_tables) == source_counts  # unchanged (no duplication)
    target.dispose()


# ---- intra-unit atomicity: a fault INSIDE a unit's txn leaves NO partial ----


class _AbortInTxn(CommunityImporter):
    """Raises inside the target table's co-commit txn (after the row insert)."""

    def __init__(self, *, boom_unit: str, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._boom_unit = boom_unit
        self._fired = False

    def _mark(self, conn: object, unit_id: str) -> None:
        if unit_id == self._boom_unit and not self._fired:
            self._fired = True
            raise _AbortError(f"aborting inside the txn of {unit_id}")
        super()._mark(conn, unit_id)


def test_intra_unit_txn_is_atomic_no_partial_rows(
    tmp_path: Path, target_url: str, embedder: HashEmbedder384
) -> None:
    sqlite_path, chroma_path, _ = _build_legacy_store(tmp_path, embedder)
    target = create_engine(target_url)

    aborted = _AbortInTxn(
        boom_unit="table:personas",
        sqlite_path=sqlite_path,
        chroma_path=chroma_path,
        target_engine=target,
        embedder=embedder,
    )
    with pytest.raises(_AbortError):
        aborted.run()

    # The personas unit's txn rolled back: NO rows AND NO journal marker (a marker
    # exists iff the rows landed — D-K10-11).
    assert _table_counts(target, ["personas"])["personas"] == 0
    assert "table:personas" not in _journal(target)

    # A clean resume imports personas exactly once.
    report = CommunityImporter(
        sqlite_path=sqlite_path,
        chroma_path=chroma_path,
        target_engine=target,
        embedder=embedder,
    ).run()
    assert report.journal_complete is True
    assert _table_counts(target, ["personas"])["personas"] == 1
    target.dispose()


# ---- clean run + nothing-to-import (fresh install) -------------------------


def test_clean_run_imports_and_renames(
    tmp_path: Path, target_url: str, embedder: HashEmbedder384
) -> None:
    sqlite_path, chroma_path, expected = _build_legacy_store(tmp_path, embedder)
    source_engine = make_community_engine(sqlite_path)
    all_tables = _all_community_table_names()
    source_counts = _table_counts(source_engine, all_tables)
    source_engine.dispose()

    target = create_engine(target_url)
    report = maybe_run_community_import(
        sqlite_path=sqlite_path,
        chroma_path=chroma_path,
        target_engine=target,
        embedder=embedder,
    )
    assert report is not None
    assert report.journal_complete is True
    assert report.units_skipped == 0  # a clean first run skips nothing
    assert _table_counts(target, all_tables) == source_counts
    assert _memory_chunk_count(target) == expected["vector_total"]
    assert not sqlite_path.exists()  # renamed away
    assert not chroma_path.exists()
    target.dispose()


def test_fresh_install_has_nothing_to_import(
    tmp_path: Path, target_url: str, embedder: HashEmbedder384
) -> None:
    # No legacy SQLite file exists → a fresh managed install; import is a no-op.
    target = create_engine(target_url)
    report = maybe_run_community_import(
        sqlite_path=tmp_path / ".persona_community.db",
        chroma_path=tmp_path / ".persona_chroma",
        target_engine=target,
        embedder=embedder,
    )
    assert report is None  # nothing to migrate
    target.dispose()


def test_import_tolerates_legacy_schema_drift_missing_columns(
    tmp_path: Path, target_url: str, embedder: HashEmbedder384
) -> None:
    # THE REAL UPGRADE SCENARIO the same-schema synthetic fixtures never exercised
    # (found by the operator pass on a real host): a legacy SQLite predating a later
    # migration — here K6's users.first_name/last_name (migration 029) — must import,
    # not crash on select(current_model). The columns absent in the SOURCE take their
    # (nullable) target defaults.
    sqlite_path, chroma_path, _ = _build_legacy_store(tmp_path, embedder)
    src = make_community_engine(sqlite_path)
    with src.begin() as conn:  # simulate a pre-K6 install
        conn.execute(text("ALTER TABLE users DROP COLUMN first_name"))
        conn.execute(text("ALTER TABLE users DROP COLUMN last_name"))
    src.dispose()

    target = create_engine(target_url)
    report = maybe_run_community_import(
        sqlite_path=sqlite_path,
        chroma_path=chroma_path,
        target_engine=target,
        embedder=embedder,
    )
    assert report is not None
    assert report.journal_complete is True
    with target.connect() as conn:
        row = conn.execute(
            text("SELECT id, first_name, last_name FROM users WHERE id = :o"),
            {"o": _OWNER},
        ).one()
    assert row.id == _OWNER
    assert row.first_name is None  # absent in the source → target default
    assert row.last_name is None
    target.dispose()
