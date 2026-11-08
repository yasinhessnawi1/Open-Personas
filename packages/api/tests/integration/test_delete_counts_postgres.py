"""The delete doors' counts on Postgres, the transport production uses (R9-284 review).

Written for CI. Uses ``pg_engine`` (tables only, no 062 trigger), so the rows need no parents.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.recall.core_memory import CoreBlock, core_block_to_chunk
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.core_memory import CoreMemoryStore
from persona.stores.document_store import DOCUMENT_STORE_KIND, DocumentStore
from persona.stores.episodic import EpisodicStore
from persona.stores.postgres import PostgresBackend
from persona.stores.self_facts import SelfFactsStore

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_PERSONA = "persona_counts"
_NOW = datetime(2026, 11, 8, tzinfo=UTC)


@pytest.fixture
def backend(pg_engine: Engine, embedder: HashEmbedder384) -> PostgresBackend:
    return PostgresBackend(engine=pg_engine, embedder=embedder)


def test_a_store_counts_every_version_it_removed(backend: PostgresBackend) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    versions = [
        PersonaChunk(
            id=chunk_id,
            text=f"text of {chunk_id}",
            created_at=_NOW,
            provenance=ChunkProvenance(
                source=WriteSource.USER,
                logical_id="fact",
                version=version,
                superseded_by=superseded_by,
                written_at=_NOW,
            ),
        )
        for chunk_id, version, superseded_by in (("v1", 1, "v2"), ("v2", 2, None))
    ]
    backend.upsert(persona_id=_PERSONA, store_kind="self_facts", chunks=versions)

    assert store.delete(_PERSONA) == 2
    assert store.delete(_PERSONA) == 0


def test_the_episodic_door_counts_each_kind_including_the_core_chain(
    backend: PostgresBackend,
) -> None:
    audit = MemoryAuditLogger()
    store = EpisodicStore(backend=backend, audit_logger=audit)
    raw = [PersonaChunk(id=f"raw-{i}", text=f"turn {i}", created_at=_NOW) for i in range(3)]
    backend.upsert(persona_id=_PERSONA, store_kind="episodic", chunks=raw)
    store.pyramid.write_gist(_PERSONA, text="gist", member_ids=[raw[0].id], created_at=_NOW)
    core = CoreMemoryStore(backend=backend, audit_logger=audit)
    for text in ("first", "second"):
        block = CoreBlock(text=text, source_ids=(raw[0].id,))
        core.write(
            _PERSONA, [core_block_to_chunk(block, _PERSONA, now=_NOW)], source=WriteSource.SYSTEM
        )

    assert store.delete_by_kind(_PERSONA) == {"episodic": 3, "episodic_gist": 1, "core_memory": 2}
    assert store.delete_by_kind(_PERSONA) == {"episodic": 0, "episodic_gist": 0, "core_memory": 0}


def test_the_document_door_counts_its_chunks(backend: PostgresBackend) -> None:
    store = DocumentStore(backend=backend)
    backend.upsert(
        persona_id="conv-1",
        store_kind=DOCUMENT_STORE_KIND,
        chunks=[PersonaChunk(id=f"d{i}", text=f"page {i}", created_at=_NOW) for i in range(2)],
    )

    assert store.delete("conv-1") == 2
    assert store.delete("conv-1") == 0
