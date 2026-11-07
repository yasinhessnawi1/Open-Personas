"""A consent grant's chunk id is minted, not computed from the chain (R9-205).

The id used to carry ``len(history) + 1``, so two writers that read the same chain built the same
physical id and the second overwrote the first in place. A partial unique index on chain heads
cannot see that: one row is one head. The id has to be unique for the index to do its job, so
this pins the real service, over a community SQLite and an in-memory chunk transport, rather than
a grep of its source.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from persona.audit import MemoryAuditLogger
from persona.errors import BrokenVersionChainError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.services import tool_consent_service

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)
_OWNER = "owner-1"
_PERSONA = "persona-1"
_TOOL = "calculator"
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: A helper for the consent id test.
tools:
  - web_search
"""


class _Chunks:
    """Holds what the store wrote. The id shape is the only thing under test."""

    def __init__(self) -> None:
        self.rows: dict[str, PersonaChunk] = {}

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        _ = (persona_id, store_kind)
        self.rows.update({chunk.id: chunk for chunk in chunks})

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        _ = (persona_id, store_kind)
        return [
            chunk
            for chunk in self.rows.values()
            if chunk.provenance is not None and chunk.provenance.logical_id in logical_ids
        ]


def _engine(tmp_path: Path) -> Engine:
    engine = make_community_engine(tmp_path / "community.db")
    create_community_schema(engine)
    ensure_owner(engine, owner_id=_OWNER, email="owner@example.test")
    with engine.begin() as conn:
        conn.execute(
            personas_t.insert().values(
                id=_PERSONA, owner_id=_OWNER, yaml=_YAML, schema_version="1.0"
            )
        )
    return engine


def _grant(engine: Engine, chunks: _Chunks, tmp_path: Path) -> bool:
    return tool_consent_service.grant_tool_consent(
        rls_engine=engine,
        embedder=None,  # type: ignore[arg-type]  # the injected transport never embeds
        audit_root=tmp_path,
        persona_id=_PERSONA,
        owner_id=_OWNER,
        tool_name=_TOOL,
        written_by=_OWNER,
        now=_NOW,
        memory_backend=chunks,  # type: ignore[arg-type]
        audit_logger=MemoryAuditLogger(),
    )


def test_the_grant_writes_a_minted_id(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    chunks = _Chunks()
    try:
        assert _grant(engine, chunks, tmp_path) is True
    finally:
        engine.dispose()

    assert len(chunks.rows) == 1
    prefix, _, tail = next(iter(chunks.rows)).rpartition("::")
    assert prefix == f"{_PERSONA}::self_facts"
    assert UUID(tail).version == 7, "the id must be minted, never computed from the chain"


def _seeded_row(chunk_id: str, version: int, superseded_by: str | None) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text="Enabled the tool.",
        metadata={"confidence": "0.95"},
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.PERSONA_SELF,
            logical_id=f"tool_consent::{_TOOL}",
            version=version,
            superseded_by=superseded_by,
            written_at=_NOW,
        ),
    )


def test_a_grant_on_a_dangling_chain_appends_a_current_version(tmp_path: Path) -> None:
    """The grant used to read ``history()`` to number its id, which raised here.

    That raise came after the YAML had already committed, so it failed a grant that had taken
    effect. The id no longer needs the read, so the audit row lands, and the chain then has a
    current version again, which ``persona repair`` can relink.
    """
    engine = _engine(tmp_path)
    chunks = _Chunks()
    chunks.rows["v1"] = _seeded_row("v1", 1, superseded_by="never-written")
    try:
        assert _grant(engine, chunks, tmp_path) is True
    finally:
        engine.dispose()

    heads = [c for c in chunks.rows.values() if c.provenance and c.provenance.superseded_by is None]
    assert len(heads) == 1
    assert heads[0].provenance is not None
    assert heads[0].provenance.version == 2


def test_a_grant_on_a_chain_with_two_heads_still_raises(tmp_path: Path) -> None:
    """Writing past two heads would pick one by accident, so the store refuses."""
    engine = _engine(tmp_path)
    chunks = _Chunks()
    chunks.rows["a"] = _seeded_row("a", 1, superseded_by=None)
    chunks.rows["b"] = _seeded_row("b", 1, superseded_by=None)
    try:
        with pytest.raises(BrokenVersionChainError):
            _grant(engine, chunks, tmp_path)
    finally:
        engine.dispose()
    assert set(chunks.rows) == {"a", "b"}, "a refused grant must not touch the chain"
