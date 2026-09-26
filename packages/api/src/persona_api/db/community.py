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

import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona.logging import get_logger
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
from sqlalchemy.schema import CreateColumn
from sqlalchemy.sql.schema import DefaultClause

from persona_api.db.models import metadata as _canonical_metadata
from persona_api.db.models import users as _users_t
from persona_api.errors import CommunitySchemaUpgradeError

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Column, Table
    from sqlalchemy.engine import Connection

__all__ = [
    "build_community_metadata",
    "create_community_schema",
    "ensure_owner",
    "make_community_engine",
    "reconcile_community_schema",
]

_LOG = get_logger("api.db.community")

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


#: Postgres-only indexes the community copy leaves out. ``Table.to_metadata`` does not carry
#: an index's ``ddl_if``, so the canonical ``ddl_if(dialect="postgresql")`` alone would still
#: build them on SQLite, where ``->>`` needs 3.38 or later (R9-158 review, MEDIUM-4).
_POSTGRES_ONLY_INDEXES = frozenset({"idx_jobs_live_task_leg"})


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
        for index in [ix for ix in copied.indexes if ix.name in _POSTGRES_ONLY_INDEXES]:
            copied.indexes.discard(index)
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


#: A default SQLite will accept in ``ALTER TABLE ... ADD COLUMN``: a literal, and
#: nothing else. ``CURRENT_TIMESTAMP``, a function call and a parenthesised
#: expression are all rejected by SQLite with "Cannot add a column with
#: non-constant default", so they are refused here with a message an operator can
#: act on instead (R9-174). The pattern is matched against the DEFAULT text the
#: SQLite dialect itself would emit, so it stays right as column types change.
_SQLITE_LITERAL_DEFAULT = re.compile(
    r"""
    ^(?:
        NULL | TRUE | FALSE
      | [+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?
      | '(?:[^']|'')*'
      | [xX]'[0-9a-fA-F]*'
    )$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def create_community_schema(engine: Engine) -> None:
    """Bring the community schema up to date, creating or upgrading (D-33-8, R9-174).

    ``metadata.create_all`` creates the tables that do not exist and, with its
    default ``checkfirst=True``, silently SKIPS every table that does. That is
    correct on a fresh file and wrong on every later boot: an existing database
    never gained a column, so upgrading across a release that added one crashed
    with a raw ``sqlite3.OperationalError: no such column`` from wherever the new
    column was first read (R9-174, the third occurrence of it). Community
    deliberately bypasses the cloud Alembic chain (it bakes in ``CREATE EXTENSION
    vector`` / ``CREATE POLICY``, both Postgres-only), so nothing else closes that
    gap. :func:`reconcile_community_schema` runs right after ``create_all`` and is
    what closes it.
    """
    community = build_community_metadata()
    community.create_all(engine)
    reconcile_community_schema(engine, metadata=community)


def reconcile_community_schema(
    engine: Engine, *, metadata: MetaData | None = None
) -> tuple[str, ...]:
    """Add the columns an existing community database is missing (R9-174).

    For every table in the community metadata, compare the model's columns against
    what SQLite actually has (``PRAGMA table_info``) and ``ALTER TABLE ... ADD
    COLUMN`` each missing one. Additive only: a column the database has but the
    model does not is left alone, and no data is ever rewritten.

    What it cannot do it refuses loudly. SQLite can only add a column, so a new
    PRIMARY KEY or UNIQUE column, a NOT NULL column with no literal default, and a
    column whose default is an expression all need a full table rebuild. This code
    does not rebuild tables; it raises :class:`CommunitySchemaUpgradeError` naming
    the table, the column and what the operator can do. Changing a column's type
    and dropping one are outside its reach for the same reason and are not
    detected: SQLite ignores declared types on read, so a type change does not
    fail here, it just keeps the old affinity.

    One divergence worth knowing: SQLite attaches foreign keys at ``CREATE TABLE``
    time only, so a reconciled column that carries one gets the column but not the
    constraint. That is logged as a warning per column rather than refused, because
    refusing would block the common, safe case for a check the single-owner
    community edition does not lean on.

    Args:
        engine: The community SQLite engine.
        metadata: The community metadata to reconcile against. Defaults to a fresh
            :func:`build_community_metadata`; the boot path passes the one it just
            created from, to avoid building it twice.

    Returns:
        The ``table.column`` names that were added, in the order they were added.

    Raises:
        CommunitySchemaUpgradeError: A missing column is a shape SQLite cannot add.
    """
    community = build_community_metadata() if metadata is None else metadata
    added: list[str] = []
    with engine.begin() as conn:
        # Plan every statement before running any of them. SQLite's pysqlite driver
        # runs DDL outside the transaction, so a refusal halfway through would leave
        # the earlier ALTERs applied; refusing during the plan is what lets the error
        # message promise that nothing was changed.
        plan: list[tuple[Table, Column[Any], str]] = []
        for table in community.sorted_tables:
            live = _live_column_names(conn, table, engine)
            if not live:
                # No rows from PRAGMA means the table does not exist. Creating tables
                # is ``create_all``'s job, not this function's.
                continue
            plan.extend(
                (table, column, _add_column_statement(engine, table, column))
                for column in table.columns
                if column.name not in live
            )
        for table, column, statement in plan:
            conn.exec_driver_sql(statement)
            added.append(f"{table.name}.{column.name}")
            if column.foreign_keys:
                _LOG.warning(
                    "community schema upgrade: added {ref} without its foreign key. "
                    "SQLite can only attach one when the table is created, so that "
                    "reference is unenforced until the database is rebuilt.",
                    ref=added[-1],
                )
    if added:
        _LOG.info(
            "community schema upgrade: added {n} missing column(s): {cols}",
            n=len(added),
            cols=", ".join(added),
        )
    return tuple(added)


def _live_column_names(conn: Connection, table: Table, engine: Engine) -> set[str]:
    """The column names SQLite actually has for ``table`` (empty if it has no table).

    ``PRAGMA`` takes no bind parameters, so the name is interpolated. It comes from
    our own metadata and is quoted by the dialect's preparer, never from a request.
    """
    quoted = engine.dialect.identifier_preparer.format_table(table)
    rows = conn.exec_driver_sql(f"PRAGMA table_info({quoted})")
    return {str(row[1]) for row in rows}


def _add_column_statement(engine: Engine, table: Table, column: Column[Any]) -> str:
    """The ``ALTER TABLE ... ADD COLUMN`` statement for one missing column.

    The column fragment is compiled from the SQLAlchemy column by the SQLite
    dialect, so a type this function has never seen renders correctly without
    anyone editing it.

    Raises:
        CommunitySchemaUpgradeError: ``column`` is a shape SQLite cannot add.
    """
    _refuse_column_sqlite_cannot_add(engine, table, column)
    quoted = engine.dialect.identifier_preparer.format_table(table)
    fragment = str(CreateColumn(column).compile(dialect=engine.dialect)).strip()
    return f"ALTER TABLE {quoted} ADD COLUMN {fragment}"


def _server_default_sql(engine: Engine, column: Column[Any]) -> str | None:
    """The DEFAULT text the dialect would emit for ``column``, or ``None`` if it has none.

    Asked of the dialect's own DDL compiler rather than rebuilt here, so the
    literal quoting matches exactly what the ADD COLUMN statement will carry.
    """
    # The compiler is annotated as needing a statement, but ``get_column_default_string``
    # only reads the column it is handed, and ``Compiled.__init__`` skips compiling when
    # the statement is None. Asking it directly beats re-deriving the literal quoting here.
    compiler = engine.dialect.ddl_compiler(engine.dialect, None)  # type: ignore[arg-type]
    return compiler.get_column_default_string(column)


def _refuse_column_sqlite_cannot_add(engine: Engine, table: Table, column: Column[Any]) -> None:
    """Raise with an actionable message if SQLite cannot ADD ``column`` (R9-174 part 2)."""
    default_sql = _server_default_sql(engine, column)
    reason: tuple[str, str] | None = None
    if column.primary_key:
        reason = ("primary_key", "belongs to the primary key, and SQLite cannot add one")
    elif column.unique:
        reason = ("unique", "is UNIQUE, and SQLite cannot add a UNIQUE column")
    elif default_sql is not None and not _SQLITE_LITERAL_DEFAULT.match(default_sql.strip()):
        reason = (
            "non_literal_default",
            f"defaults to {default_sql.strip()}, which is not a literal, and SQLite can only "
            "add a column whose default is one",
        )
    elif not column.nullable and default_sql is None:
        reason = (
            "not_null_without_default",
            "is NOT NULL with no database default, so the rows already in the table would have "
            "no value for it, and SQLite refuses that",
        )
    if reason is None:
        return
    code, explanation = reason
    database = engine.url.database or str(engine.url)
    raise CommunitySchemaUpgradeError(
        f"this community database is older than the code and cannot be upgraded in place: "
        f"column {table.name}.{column.name} {explanation}. Nothing was changed. Two ways "
        f"forward: set PERSONA_COMMUNITY_DB_MODE=auto, which runs the real migration chain "
        f"on the managed database and imports this file into it, keeping your data; or move "
        f"{database} aside and start again on a fresh database, which loses what is in it.",
        context={
            "reason": code,
            "table": table.name,
            "column": column.name,
            "database": database,
        },
    )


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
