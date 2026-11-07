"""Core and the api name the head index separately, and they have to agree (R9-205).

``persona-core`` cannot import the api's schema, so ``PostgresBackend`` recognises the violation by
a name of its own. If the two drifted, nothing would fail at a write: the second writer's
``IntegrityError`` would simply stop being a ``ChainConflictError`` and the retry would go dark.
The migration is checked too, because a deployed database gets its index from there and a fresh
one from the model, so a difference between them is a difference between environments.
"""

from __future__ import annotations

import re
from pathlib import Path

from persona.stores.postgres import CHAIN_HEAD_INDEX
from persona_api.db.models import memory_chunks
from sqlalchemy import Index

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "059_memory_chain_head_unique.py"
)
_PREDICATE = "superseded_by IS NULL AND logical_id IS NOT NULL"


def _index() -> Index:
    return next(i for i in memory_chunks.indexes if i.name == CHAIN_HEAD_INDEX)


def test_the_model_declares_the_index_core_recognises() -> None:
    index = _index()
    assert index.unique
    assert [c.name for c in index.columns] == ["persona_id", "kind", "logical_id"]
    assert str(index.dialect_options["postgresql"]["where"]) == _PREDICATE


def test_the_migration_builds_the_same_index() -> None:
    source = re.sub(r"\s+", " ", _MIGRATION.read_text(encoding="utf-8").replace('" "', ""))
    assert f"CREATE UNIQUE INDEX IF NOT EXISTS {CHAIN_HEAD_INDEX}" in source
    assert "ON memory_chunks (persona_id, kind, logical_id)" in source
    assert f"WHERE {_PREDICATE}" in source
    assert f"DROP INDEX IF EXISTS {CHAIN_HEAD_INDEX}" in source
