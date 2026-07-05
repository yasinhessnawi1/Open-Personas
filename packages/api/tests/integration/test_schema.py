"""Schema shape tests (spec 07, T03) — the Core MetaData builds correctly.

These run against real Postgres (the ``pg_engine`` fixture builds the schema via
``MetaData.create_all``). They assert the table set, the promoted
``memory_chunks`` versioning columns (D-07-4), and the index set (incl. the HNSW
and partial current-heads indexes). The full Alembic migration is tested
separately (T07 ``test_migration.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona_api.db import EMBEDDING_DIM, STORE_KINDS
from sqlalchemy import inspect

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_EXPECTED_TABLES = {
    "users",
    "personas",
    "conversations",
    "messages",
    "runs",
    "memory_chunks",
    "turn_logs",
    "rate_limit_buckets",
    "credits",
    "credit_transactions",
    "audit_log",
}

_EXPECTED_MEMORY_COLS = {
    "id",
    "persona_id",
    "kind",
    "text",
    "embedding",
    "embedding_model",
    "content_hash",
    "metadata",
    "logical_id",
    "version",
    "superseded_by",
    "prov_source",
    "written_at",
    "written_by",
    "reason",
    "created_at",
    # Spec K8 lifecycle columns (K8-D-2/13).
    "strength",
    "last_recalled_at",
    "fidelity_band",
    "pinned",
    "member_ids",
}


def test_all_tables_created(pg_engine: Engine) -> None:
    insp = inspect(pg_engine)
    assert _EXPECTED_TABLES.issubset(set(insp.get_table_names()))


def test_memory_chunks_promotes_versioning_columns(pg_engine: Engine) -> None:
    insp = inspect(pg_engine)
    cols = {c["name"] for c in insp.get_columns("memory_chunks")}
    assert cols == _EXPECTED_MEMORY_COLS
    # decay_t0 from the spec's §5 sketch is intentionally dropped (D-07-4).
    assert "decay_t0" not in cols


def test_memory_chunks_indexes(pg_engine: Engine) -> None:
    insp = inspect(pg_engine)
    names = {i["name"] for i in insp.get_indexes("memory_chunks")}
    assert {
        "idx_memory_persona_kind",
        "idx_memory_persona_kind_logical",
        "idx_memory_current_heads",
        "idx_memory_embedding",
        "idx_memory_persona_kind_created",  # Spec K8: the recent() pushdown index
    }.issubset(names)


def test_embedding_dim_constant() -> None:
    assert EMBEDDING_DIM == 384
    assert STORE_KINDS == ("identity", "self_facts", "worldview", "episodic")
