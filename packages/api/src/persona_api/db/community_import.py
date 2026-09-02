"""Community legacy → managed-Postgres auto-import (Spec K10, T6).

Moves an existing community install's data (the legacy SQLite relational file +
the Chroma vector directory) into the managed Postgres substrate, INVISIBLY and
CRASH-SAFELY (D-K10-4 / D-K10-11). The invariant that makes it safe to re-run
after any interruption:

    **Import journal INSIDE the target Postgres, one transaction per unit.**

Every import *unit* (one relational table, or one ``(id, store_kind)`` vector
collection) records a completion marker in the ``k10_import_journal`` table. A
relational unit writes its rows AND its marker in the **same transaction**, so a
marker exists iff the rows landed — a crash mid-unit rolls back both, and the
re-run redoes the unit cleanly (exactly-once). A vector unit re-embeds through
``PostgresBackend.upsert`` (``ON CONFLICT (id) DO UPDATE`` — idempotent by chunk
id), so re-running a vector unit is a safe no-op; its marker is written right
after. On resume, any unit already in the journal is skipped.

The legacy sources are renamed to ``*.migrated-<timestamp>`` (the rollback
marker) **only after the journal shows every unit complete** — so an interrupted
import always leaves the sources in place and simply resumes.

Relational rows are copied in FK order (``build_community_metadata().sorted_tables``);
vectors are re-embedded (no raw-vector porting — the ``Backend`` transport embeds
on write) with ids sourced from the SQLite ``personas`` / ``conversations`` tables,
never parsed back from the (lossy-sanitised) Chroma collection names.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from persona.logging import get_logger
from sqlalchemy import Table, create_engine, select, text
from sqlalchemy import inspect as sa_inspect

from persona_api.db.community import build_community_metadata, make_community_engine
from persona_api.db.models import metadata as _canonical_metadata

if TYPE_CHECKING:
    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine

__all__ = ["CommunityImporter", "ImportReport", "maybe_run_community_import"]

_LOG = get_logger("api.db.community_import")

_JOURNAL_TABLE = "k10_import_journal"

#: Vector store kinds keyed by ``persona_id`` (the 4 typed stores + the K8 gist tier).
_PERSONA_VECTOR_KINDS = ("identity", "worldview", "self_facts", "episodic", "episodic_gist")
#: The document store keys its chunks by ``conversation_id`` (Spec 14).
_DOCUMENT_KIND = "document"


@dataclass
class ImportReport:
    """What one import run did (a resume reports only the units IT completed)."""

    units_total: int = 0
    units_imported: int = 0
    units_skipped: int = 0  # already journalled by a prior run
    table_rows: dict[str, int] = field(default_factory=dict)
    vector_chunks: dict[str, int] = field(default_factory=dict)
    journal_complete: bool = False
    renamed: list[str] = field(default_factory=list)


class CommunityImporter:
    """Crash-safe legacy → managed-Postgres importer (Spec K10, T6 / D-K10-11).

    Construct with the legacy source paths, the target (managed) engine, and the
    embedder used to re-embed vector chunks. Call :meth:`run` — it is idempotent
    and resumable: run it again after any interruption and every unit ends up
    imported exactly once.
    """

    def __init__(
        self,
        *,
        sqlite_path: Path,
        chroma_path: Path,
        target_engine: Engine,
        embedder: Embedder,
    ) -> None:
        self._sqlite_path = sqlite_path
        self._chroma_path = chroma_path
        self._target = target_engine
        self._embedder = embedder
        self._community_metadata = build_community_metadata()

    # ----- public API ------------------------------------------------------

    def has_legacy_data(self) -> bool:
        """Whether a legacy community store exists to import (the SQLite file)."""
        return self._sqlite_path.exists()

    def run(self) -> ImportReport:
        """Import (or resume) every unit exactly once; rename sources when complete."""
        report = ImportReport()
        if not self.has_legacy_data():
            report.journal_complete = True  # nothing to do — a fresh install
            _LOG.info(
                "community import: no legacy SQLite store at {p} — nothing to import",
                p=str(self._sqlite_path),
            )
            return report

        self._ensure_journal()
        done = self._completed_units()
        source = make_community_engine(self._sqlite_path)
        try:
            expected: set[str] = set()
            self._import_tables(source, done, expected, report)
            self._import_vectors(source, done, expected, report)
        finally:
            source.dispose()

        report.units_total = len(expected)
        # Rename the sources ONLY once the journal shows every unit complete.
        if expected <= self._completed_units():
            report.journal_complete = True
            report.renamed = self._rename_sources()
        _LOG.info(
            "community import finished: imported={i} skipped={s} total={t} complete={c}",
            i=report.units_imported,
            s=report.units_skipped,
            t=report.units_total,
            c=report.journal_complete,
        )
        return report

    # ----- relational leg (co-committed rows + marker) ---------------------

    def _import_tables(
        self, source: Engine, done: set[str], expected: set[str], report: ImportReport
    ) -> None:
        for community_table in self._community_metadata.sorted_tables:  # FK order
            unit_id = f"table:{community_table.name}"
            expected.add(unit_id)
            if unit_id in done:
                report.units_skipped += 1
                continue
            rows = self._read_table(source, community_table)
            self._write_table_unit(community_table.name, rows, unit_id)
            report.table_rows[community_table.name] = len(rows)
            report.units_imported += 1
            self._on_unit_committed(unit_id)  # test seam: a real abort point

    def _read_table(self, source: Engine, community_table: Table) -> list[dict[str, object]]:
        # A legacy SQLite may predate columns/tables added by later migrations (e.g.
        # K6's users.first_name/last_name, migration 029). Read only what the SOURCE
        # actually has — reflect its columns and select the intersection with the
        # canonical table; the target's newer columns take their (nullable) defaults.
        # A table absent from the source (added after this legacy install existed)
        # yields no rows. This keeps the upgrade robust to cross-version schema drift.
        inspector = sa_inspect(source)
        if not inspector.has_table(community_table.name):
            return []
        source_cols = {c["name"] for c in inspector.get_columns(community_table.name)}
        cols = [c for c in community_table.columns if c.name in source_cols]
        if not cols:
            return []
        with source.connect() as conn:
            return [dict(r) for r in conn.execute(select(*cols)).mappings().all()]

    def _write_table_unit(self, name: str, rows: list[dict[str, object]], unit_id: str) -> None:
        """One transaction: the table's rows AND its journal marker (D-K10-11).

        Atomic — a crash mid-unit rolls back both, so a marker exists iff the rows
        landed and the re-run redoes the unit cleanly.
        """
        canonical = _canonical_metadata.tables[name]
        with self._target.begin() as conn:
            if rows:
                conn.execute(canonical.insert(), rows)
            self._mark(conn, unit_id)

    # ----- vector leg (re-embed via idempotent upsert; marker after) -------

    def _import_vectors(
        self, source: Engine, done: set[str], expected: set[str], report: ImportReport
    ) -> None:
        if not self._chroma_path.exists():
            return  # relational-only legacy install (no vectors)
        from persona.stores.chroma import ChromaBackend
        from persona.stores.postgres import PostgresBackend

        chroma = ChromaBackend(persist_path=self._chroma_path, embedder=self._embedder)
        target_backend = PostgresBackend(engine=self._target, embedder=self._embedder)

        for owner_id, kind in self._vector_units(source):
            unit_id = f"vector:{kind}:{owner_id}"
            chunks = chroma.get_all(persona_id=owner_id, store_kind=kind)
            if not chunks:
                continue  # empty collection — not a unit
            expected.add(unit_id)
            if unit_id in done:
                report.units_skipped += 1
                continue
            # Re-embed via the SAME write path the app uses (ON CONFLICT (id) DO
            # UPDATE → idempotent; re-running after a crash is a safe no-op).
            target_backend.upsert(persona_id=owner_id, store_kind=kind, chunks=chunks)
            with self._target.begin() as conn:
                self._mark(conn, unit_id)
            report.vector_chunks[unit_id] = len(chunks)
            report.units_imported += 1
            self._on_unit_committed(unit_id)

    def _vector_units(self, source: Engine) -> list[tuple[str, str]]:
        """(``id``, ``store_kind``) pairs, ids taken from SQLite (never name-parsed).

        Persona-keyed kinds use the ``personas`` ids; the document kind uses the
        ``conversations`` ids (Spec 14 keys document chunks by conversation).
        """
        personas_t = self._community_metadata.tables["personas"]
        conversations_t = self._community_metadata.tables["conversations"]
        units: list[tuple[str, str]] = []
        with source.connect() as conn:
            persona_ids = list(conn.execute(select(personas_t.c.id)).scalars().all())
            conversation_ids = list(conn.execute(select(conversations_t.c.id)).scalars().all())
        for pid in persona_ids:
            units.extend((pid, kind) for kind in _PERSONA_VECTOR_KINDS)
        units.extend((cid, _DOCUMENT_KIND) for cid in conversation_ids)
        return units

    # ----- journal + rename ------------------------------------------------

    def _ensure_journal(self) -> None:
        with self._target.begin() as conn:
            conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {_JOURNAL_TABLE} ("
                    "unit_id TEXT PRIMARY KEY, "
                    "completed_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )

    def _completed_units(self) -> set[str]:
        with self._target.connect() as conn:
            return set(conn.execute(text(f"SELECT unit_id FROM {_JOURNAL_TABLE}")).scalars().all())

    def _mark(self, conn: object, unit_id: str) -> None:
        # ``conn`` is an open Connection inside a transaction (typed loosely so the
        # co-commit call site can pass its own transactional connection).
        conn.execute(  # type: ignore[attr-defined]
            text(
                f"INSERT INTO {_JOURNAL_TABLE} (unit_id) VALUES (:u) "
                "ON CONFLICT (unit_id) DO NOTHING"
            ),
            {"u": unit_id},
        )

    def _rename_sources(self) -> list[str]:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        renamed: list[str] = []
        for path in (self._sqlite_path, self._chroma_path):
            if path.exists():
                dest = path.with_name(f"{path.name}.migrated-{stamp}")
                path.rename(dest)
                renamed.append(str(dest))
        return renamed

    def _on_unit_committed(self, unit_id: str) -> None:  # noqa: ARG002 — test seam
        """Called after each unit commits — a seam for the crash-resume test to abort."""


def maybe_run_community_import(
    *, sqlite_path: Path, chroma_path: Path, target_engine: Engine, embedder: Embedder
) -> ImportReport | None:
    """Run the import iff a legacy store is present; return ``None`` for a fresh install.

    The invisible-boot entry point (wired into the managed ``auto`` branch in T8):
    detect legacy data → import (resumable) → return the report; no legacy data →
    ``None`` (a fresh managed install, nothing to migrate).
    """
    importer = CommunityImporter(
        sqlite_path=sqlite_path,
        chroma_path=chroma_path,
        target_engine=target_engine,
        embedder=embedder,
    )
    if not importer.has_legacy_data():
        return None
    return importer.run()


def main(argv: list[str] | None = None) -> int:
    """Manual escape hatch: ``python -m persona_api.db.community_import`` (D-K10-4)."""
    parser = argparse.ArgumentParser(
        description="Import a legacy community store into managed Postgres."
    )
    parser.add_argument("--sqlite", required=True, type=Path, help="Path to .persona_community.db")
    parser.add_argument("--chroma", required=True, type=Path, help="Path to .persona_chroma dir")
    parser.add_argument("--database-url", required=True, help="Target Postgres DSN (managed)")
    args = parser.parse_args(argv)

    from persona_api.config import APIConfig
    from persona_api.services import persona_service

    url = args.database_url.replace("+asyncpg", "+psycopg")
    engine = create_engine(url)
    try:
        report = maybe_run_community_import(
            sqlite_path=args.sqlite,
            chroma_path=args.chroma,
            target_engine=engine,
            embedder=persona_service.default_embedder(APIConfig().embedder_model),
        )
    finally:
        engine.dispose()
    if report is None:
        sys.stdout.write("no legacy community store found, nothing to import\n")
        return 0
    sys.stdout.write(
        f"import complete={report.journal_complete} imported={report.units_imported} "
        f"skipped={report.units_skipped} total={report.units_total} renamed={report.renamed}\n"
    )
    return 0 if report.journal_complete else 1


if __name__ == "__main__":  # pragma: no cover — manual CLI entry
    raise SystemExit(main())
