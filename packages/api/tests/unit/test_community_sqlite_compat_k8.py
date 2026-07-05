"""Spec K8 community-SQLite compat (the R4-C1 bar; 712cb2a pattern, K8-D-13).

K8 adds Postgres-isms to the canonical ``memory_chunks`` declaration —
``member_ids TEXT[]`` (``postgresql.ARRAY``) plus lifecycle columns. Community
runs SQLite, where ARRAY does not exist. The design answer (K8-D-2): community
DROPS ``memory_chunks`` from its metadata entirely (typed-memory vectors live
in Chroma there), so the ARRAY column never reaches SQLite.

These tests make that PROVEN, not assumed — if the community transform ever
stops dropping the table (or a K8 column leaks into a table community DOES
keep), ``create_community_schema`` on a real SQLite engine fails here first,
not in a user's zero-infra install. (Named in a separate file from main's
``test_community_sqlite_compat.py`` to stay merge-safe; same pattern.)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_api.db.community import (
    build_community_metadata,
    create_community_schema,
    make_community_engine,
)
from sqlalchemy import inspect

if TYPE_CHECKING:
    from pathlib import Path


def test_memory_chunks_is_dropped_from_the_community_metadata() -> None:
    # The structural guard: the table carrying ARRAY(member_ids) must never be
    # part of the community (SQLite) schema — vectors live in Chroma there.
    md = build_community_metadata()
    assert "memory_chunks" not in md.tables


def test_community_schema_builds_on_real_sqlite_with_the_k8_columns_canonical(
    tmp_path: Path,
) -> None:
    # End-to-end: with the K8 canonical columns present in models.py, the
    # community transform still compiles + creates on a REAL SQLite engine.
    # Any new Postgres-ism leaking past the drop fails right here.
    engine = make_community_engine(tmp_path / "k8-compat.db")
    create_community_schema(engine)
    tables = set(inspect(engine).get_table_names())
    assert "memory_chunks" not in tables
    assert "personas" in tables  # the rest of the schema built normally
