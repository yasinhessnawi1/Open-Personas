"""Migration 059: one current version per chain, or a refusal that changes nothing (R9-205).

This migration first runs against a restored production dump, on data last checked weeks before
the backup was taken. So the property under test is not only that the index appears. It is that a
database carrying a chain with two heads comes out of a failed upgrade EXACTLY as it went in: no
index, the old revision, every row untouched, and a message saying what to do. Nothing is
repaired here, because a schema step has no dry run and no audit trail, and the repair that does
have both is ``persona repair``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.postgres import CHAIN_HEAD_INDEX, PostgresBackend
from persona_api.errors import ChainHeadIndexInvalidError, ChainHeadsConflictError
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]
_PRED = "058_jobs_live_task_leg_index"
_HEAD = "059_memory_chain_head_unique"
_NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
_LOGICAL = "chain"


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _reset_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def _execute(database_url: str, sql: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()


def _scalar(database_url: str, sql: str) -> object:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def _rows(database_url: str) -> list[tuple[object, ...]]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            got = conn.execute(
                text(
                    "SELECT id, persona_id, kind, text, content_hash, logical_id, version, "
                    "superseded_by FROM memory_chunks ORDER BY id"
                )
            )
            return [tuple(row) for row in got]
    finally:
        engine.dispose()


def _index_definition(database_url: str) -> str | None:
    found = _scalar(
        database_url,
        f"SELECT indexdef FROM pg_indexes WHERE indexname = '{CHAIN_HEAD_INDEX}'",
    )
    return None if found is None else str(found)


def _chunk(
    chunk_id: str, version: int, superseded_by: str | None, logical_id: str = _LOGICAL
) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=f"text of {chunk_id}",
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=logical_id,
            version=version,
            superseded_by=superseded_by,
            written_at=_NOW,
        ),
    )


def _seed(database_url: str, embedder: HashEmbedder384, chunks: list[PersonaChunk]) -> None:
    engine = create_engine(database_url)
    try:
        PostgresBackend(engine=engine, embedder=embedder).upsert(
            persona_id="p1", store_kind="self_facts", chunks=chunks
        )
    finally:
        engine.dispose()


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    _reset_schema(database_url)
    yield database_url
    _reset_schema(database_url)


@pytest.fixture
def deployed_db(clean_db: str) -> str:
    """A database at 058 with no index, which is what the restored dump is.

    001 builds from the canonical metadata, so a fresh chain has the index from the start.
    """
    command.upgrade(_alembic_config(clean_db), _PRED)
    _execute(clean_db, f"DROP INDEX IF EXISTS {CHAIN_HEAD_INDEX}")
    return clean_db


def _revision(database_url: str) -> object:
    return _scalar(database_url, "SELECT version_num FROM alembic_version")


@pytest.mark.timeout(300)
def test_a_fresh_chain_reaches_head_with_the_index(clean_db: str) -> None:
    command.upgrade(_alembic_config(clean_db), "head")
    assert _revision(clean_db) == _HEAD
    definition = _index_definition(clean_db)
    assert definition is not None
    assert "CREATE UNIQUE INDEX" in definition
    assert "(persona_id, kind, logical_id)" in definition
    assert "superseded_by IS NULL" in definition
    assert "logical_id IS NOT NULL" in definition


@pytest.mark.timeout(300)
def test_a_clean_deployed_database_gains_it_and_downgrade_removes_it(
    deployed_db: str, embedder: HashEmbedder384
) -> None:
    _seed(deployed_db, embedder, [_chunk("v1", 1, "v2"), _chunk("v2", 2, None)])
    cfg = _alembic_config(deployed_db)

    command.upgrade(cfg, "head")
    assert _index_definition(deployed_db) is not None
    assert _scalar(
        deployed_db,
        f"SELECT indisvalid FROM pg_index WHERE indexrelid = '{CHAIN_HEAD_INDEX}'::regclass",
    )

    command.downgrade(cfg, _PRED)
    assert _index_definition(deployed_db) is None
    assert _revision(deployed_db) == _PRED


@pytest.mark.timeout(300)
def test_two_heads_refuse_and_the_database_comes_out_exactly_as_it_went_in(
    deployed_db: str, embedder: HashEmbedder384
) -> None:
    _seed(deployed_db, embedder, [_chunk("v1", 1, "b"), _chunk("a", 2, None), _chunk("b", 2, None)])
    before = _rows(deployed_db)

    with pytest.raises(ChainHeadsConflictError) as caught:
        command.upgrade(_alembic_config(deployed_db), "head")

    assert _index_definition(deployed_db) is None, "a refused migration left an index behind"
    assert _revision(deployed_db) == _PRED, "a refused migration moved the revision"
    assert _rows(deployed_db) == before, "a refused migration changed a row"
    assert (
        _scalar(deployed_db, "SELECT count(*) FROM pg_indexes WHERE indexdef LIKE '%INVALID%'") == 0
    )

    message = str(caught.value)
    assert "persona repair --all-personas" in message, "it has to say what to do"
    assert "self_facts" in message, "and where to look"
    assert "text of" not in message, "never row content: this reaches a deploy log"


@pytest.mark.timeout(300)
def test_it_counts_chains_by_kind(deployed_db: str, embedder: HashEmbedder384) -> None:
    _seed(
        deployed_db,
        embedder,
        [
            _chunk("a1", 1, None, "first"),
            _chunk("a2", 1, None, "first"),
            _chunk("b1", 1, None, "second"),
            _chunk("b2", 1, None, "second"),
        ],
    )
    with pytest.raises(ChainHeadsConflictError) as caught:
        command.upgrade(_alembic_config(deployed_db), "head")
    assert caught.value.context["chains"] == "2"


@pytest.mark.timeout(300)
def test_a_retry_after_the_repair_goes_through(deployed_db: str, embedder: HashEmbedder384) -> None:
    from persona.audit import MemoryAuditLogger
    from persona.stores.self_facts import SelfFactsStore

    _seed(deployed_db, embedder, [_chunk("v1", 1, "b"), _chunk("a", 2, None), _chunk("b", 2, None)])
    cfg = _alembic_config(deployed_db)
    with pytest.raises(ChainHeadsConflictError):
        command.upgrade(cfg, "head")

    engine = create_engine(deployed_db)
    try:
        store = SelfFactsStore(
            backend=PostgresBackend(engine=engine, embedder=embedder),
            audit_logger=MemoryAuditLogger(),
        )
        assert store.repair("p1", resolve_raced=True) == 1
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    assert _revision(deployed_db) == _HEAD
    assert _index_definition(deployed_db) is not None


@pytest.mark.timeout(300)
def test_chains_that_block_nothing_do_not_block_the_upgrade(
    deployed_db: str, embedder: HashEmbedder384
) -> None:
    """A dangling link or a gap is worth repairing, but neither stops the index being built."""
    _seed(
        deployed_db,
        embedder,
        [
            _chunk("dangling", 1, "never-written", "one"),
            _chunk("gap-1", 1, "gap-3", "two"),
            _chunk("gap-3", 3, None, "two"),
        ],
    )
    command.upgrade(_alembic_config(deployed_db), "head")
    assert _revision(deployed_db) == _HEAD


@pytest.mark.timeout(300)
def test_rows_with_no_chain_are_outside_the_index(
    deployed_db: str, embedder: HashEmbedder384
) -> None:
    """Identity and document rows have no logical id, and there are many of them."""
    engine = create_engine(deployed_db)
    try:
        PostgresBackend(engine=engine, embedder=embedder).upsert(
            persona_id="p1",
            store_kind="identity",
            chunks=[PersonaChunk(id=f"i{n}", text=f"line {n}", created_at=_NOW) for n in range(3)],
        )
    finally:
        engine.dispose()
    command.upgrade(_alembic_config(deployed_db), "head")
    assert _revision(deployed_db) == _HEAD


@pytest.mark.timeout(300)
def test_an_invalid_index_of_that_name_refuses_rather_than_passes(
    deployed_db: str, embedder: HashEmbedder384
) -> None:
    """Round 2 review, LOW-5. A failed concurrent build leaves an INVALID index behind."""
    _seed(deployed_db, embedder, [_chunk("v1", 1, "b"), _chunk("a", 2, None), _chunk("b", 2, None)])
    autocommit = create_engine(deployed_db, isolation_level="AUTOCOMMIT")
    try:
        with autocommit.connect() as conn, pytest.raises(Exception, match="duplicate|unique"):
            conn.execute(
                text(
                    f"CREATE UNIQUE INDEX CONCURRENTLY {CHAIN_HEAD_INDEX} "
                    "ON memory_chunks (persona_id, kind, logical_id) "
                    "WHERE superseded_by IS NULL AND logical_id IS NOT NULL"
                )
            )
        with autocommit.connect() as conn:
            conn.execute(text("DELETE FROM memory_chunks WHERE id = 'a'"))  # data clean now
    finally:
        autocommit.dispose()
    assert (
        _scalar(
            deployed_db,
            f"SELECT indisvalid FROM pg_index WHERE indexrelid = '{CHAIN_HEAD_INDEX}'::regclass",
        )
        is False
    )

    with pytest.raises(ChainHeadIndexInvalidError) as caught:
        command.upgrade(_alembic_config(deployed_db), "head")

    assert _revision(deployed_db) == _PRED
    assert f"DROP INDEX {CHAIN_HEAD_INDEX}" in str(caught.value)

    _execute(deployed_db, f"DROP INDEX {CHAIN_HEAD_INDEX}")
    command.upgrade(_alembic_config(deployed_db), "head")
    assert (
        _scalar(
            deployed_db,
            f"SELECT indisvalid FROM pg_index WHERE indexrelid = '{CHAIN_HEAD_INDEX}'::regclass",
        )
        is True
    )
