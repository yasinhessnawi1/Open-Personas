"""Community edition persistence: SQLite, no RLS (Spec 33, Cluster B).

The community edition runs zero-infra: a single SQLite file for the relational
data and Chroma for the typed-memory vectors. This module builds the community
view of the schema and the engine the request path runs on.

Three transforms turn the canonical (Postgres) :data:`persona_api.db.models`
metadata into a SQLite-viable community metadata (D-33-7, proven in R-33-1):

1. **Drop ``memory_chunks``** — typed-memory vectors live in Chroma in
   community (``ChromaBackend``), never in a relational table, so the
   pgvector/HNSW column never reaches SQLite (D-33-X-memory-chroma-community).
2. **JSON** — handled upstream: ``models._json()`` already emits the generic
   ``JSON`` type on SQLite via ``with_variant`` (D-33-X-json-variant).
3. **Dialect-aware UUID** — replace the ``gen_random_uuid()::text`` server
   default (Postgres-only) with a client-side UUID default, so the canonical
   ``models.py`` is untouched and the cloud DDL is byte-identical
   (D-33-X-uuid-dialect-aware).

The engine has **no RLS pool listener** (community is single-owner — the
constant ``owner_id`` is the only tenant) but DOES install a ``connect``
listener enabling ``PRAGMA foreign_keys=ON`` — SQLite enforces foreign keys
(including the composite cross-tenant-defence FKs) only when that pragma is set
(proven in R-33-1).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    ColumnDefault,
    DateTime,
    MetaData,
    TypeDecorator,
    create_engine,
    event,
    insert,
    select,
)
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.sql.schema import DefaultClause

from persona_api.db.models import metadata as _canonical_metadata
from persona_api.db.models import users as _users_t

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "build_community_metadata",
    "create_community_schema",
    "ensure_owner",
    "make_community_engine",
]

# Tables that exist only in the cloud relational store — never part of the
# community SQLite store. ``memory_chunks``' vectors live in Chroma in community;
# the Spec K0 graph tables carry pgvector ``Vector`` + ``tsvector`` columns
# (Postgres-only) and the graph is a cloud feature, so they are excluded too. Spec
# K2's ``synthesis_markers`` is the graph-synthesis idempotency surface — useless
# without the graph, so it is cloud-only as well.
_CLOUD_ONLY_TABLES = frozenset(
    {
        "memory_chunks",
        "graph_nodes",
        "graph_edges",
        "graph_entities",
        "graph_node_entities",
        "graph_node_versions",
        "graph_consolidation_markers",
        "synthesis_markers",
    }
)


def _new_uuid() -> str:
    """A client-side UUID string (the community PK default)."""
    return str(uuid.uuid4())


class _SqliteUTCDateTime(TypeDecorator[datetime]):
    """Return tz-aware UTC datetimes from SQLite (community parity with cloud).

    SQLite has no native timezone type, so ``DateTime(timezone=True)`` silently
    stores/returns NAIVE datetimes — which trips the project's tz-aware validators
    (e.g. ``ConversationMessage.created_at``) the moment a prior row is read back,
    a community-only divergence from cloud's TIMESTAMPTZ. The codebase writes
    UTC-aware datetimes everywhere, so attach UTC on read and normalise any aware
    input to UTC on write. ``impl = DateTime`` keeps the emitted DDL identical.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.astimezone(UTC)
        return value

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


def build_community_metadata() -> MetaData:
    """The SQLite-viable community view of the schema (Spec 33, D-33-7).

    Copies every canonical table except the cloud-only ones, then rewrites the
    Postgres ``gen_random_uuid()::text`` server defaults to a client-side UUID
    default. The canonical ``models.py`` is never mutated.
    """
    target = MetaData()
    for table in _canonical_metadata.sorted_tables:
        if table.name in _CLOUD_ONLY_TABLES:
            continue
        copied = table.to_metadata(target)
        for column in copied.columns:
            server_default = column.server_default
            if isinstance(server_default, DefaultClause) and "gen_random_uuid" in str(
                server_default.arg
            ):
                # Postgres generates the PK server-side; SQLite has no such
                # function. Generate it client-side instead (D-33-X-uuid-dialect-aware).
                column.server_default = None
                column.default = ColumnDefault(_new_uuid)
            # SQLite ignores DateTime(timezone=True) and returns naive datetimes,
            # which trip the tz-aware validators on read. Wrap so community reads
            # come back UTC-aware, matching cloud's TIMESTAMPTZ (the 4th transform).
            if isinstance(column.type, DateTime):
                column.type = _SqliteUTCDateTime()
    return target


def make_community_engine(db_path: Path) -> Engine:
    """A SQLite engine for the community relational store (Spec 33, D-33-X-community-engine).

    Satisfies the same ``app.state.rls_engine`` contract the services consume,
    but with NO RLS pool listener (single owner → no multi-tenant scoping) and a
    ``connect`` listener enabling ``PRAGMA foreign_keys=ON`` so the schema's FKs
    (incl. the composite cross-tenant-defence constraints) are enforced.
    """
    engine = create_engine(f"sqlite+pysqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_conn: Any, _record: Any) -> None:  # noqa: ANN401 - DBAPI conn
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def create_community_schema(engine: Engine) -> None:
    """Create the community schema on a fresh SQLite file (D-33-8).

    Uses ``metadata.create_all`` over the community-variant metadata, bypassing
    the cloud Alembic RLS chain entirely (the chain bakes in
    ``CREATE EXTENSION vector`` / ``CREATE POLICY``, both Postgres-only).
    """
    build_community_metadata().create_all(engine)


def ensure_owner(engine: Engine, *, owner_id: str, email: str) -> None:
    """Seed the fixed single owner row, idempotently (Spec 33, D-33-X-owner-seed).

    The app-table FKs (``personas.owner_id`` → ``users.id`` and the composite
    FKs) require the owner row to exist before any request is served. This
    replaces cloud's JIT ``ensure_user`` (which needs the superuser engine).
    """
    with engine.begin() as conn:
        exists = conn.execute(select(_users_t.c.id).where(_users_t.c.id == owner_id)).first()
        if exists is None:
            conn.execute(insert(_users_t).values(id=owner_id, email=email))
