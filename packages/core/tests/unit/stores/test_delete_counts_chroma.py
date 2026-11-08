"""The delete doors' counts on a real transport (R9-284 review).

The other count tests use in-memory fakes, which agree with whatever the door assumes. This writes
through real Chroma, so the numbers are what the store really held, superseded versions included.
The same checks on Postgres are in ``test_delete_counts_postgres.py`` in the api integration suite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - used at runtime in fixtures

import pytest
from persona.audit import MemoryAuditLogger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.chroma import ChromaBackend
from persona.stores.document_store import DOCUMENT_STORE_KIND, DocumentStore
from persona.stores.embedder import HashEmbedder
from persona.stores.episodic import EpisodicStore
from persona.stores.self_facts import SelfFactsStore

_PERSONA = "persona_counts"
_NOW = datetime(2026, 11, 8, tzinfo=UTC)


@pytest.fixture
def backend(tmp_path: Path) -> ChromaBackend:
    pytest.importorskip("chromadb")
    return ChromaBackend(persist_path=tmp_path / "chroma", embedder=HashEmbedder())


def _chunk(chunk_id: str, *, version: int = 1, superseded_by: str | None = None) -> PersonaChunk:
    return PersonaChunk(
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


def test_a_store_counts_every_version_it_removed(backend: ChromaBackend) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    backend.upsert(
        persona_id=_PERSONA,
        store_kind="self_facts",
        chunks=[_chunk("v1", version=1, superseded_by="v2"), _chunk("v2", version=2)],
    )

    assert store.delete(_PERSONA) == 2
    assert backend.get_all(persona_id=_PERSONA, store_kind="self_facts") == []
    assert store.delete(_PERSONA) == 0


def test_the_episodic_door_counts_each_kind(backend: ChromaBackend) -> None:
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    raw = [PersonaChunk(id=f"raw-{i}", text=f"turn {i}", created_at=_NOW) for i in range(3)]
    backend.upsert(persona_id=_PERSONA, store_kind="episodic", chunks=raw)
    store.pyramid.write_gist(_PERSONA, text="gist", member_ids=[raw[0].id], created_at=_NOW)

    assert store.delete_by_kind(_PERSONA) == {"episodic": 3, "episodic_gist": 1, "core_memory": 0}
    assert store.delete_by_kind(_PERSONA) == {"episodic": 0, "episodic_gist": 0, "core_memory": 0}


def test_the_document_door_counts_its_chunks(backend: ChromaBackend) -> None:
    store = DocumentStore(backend=backend)
    backend.upsert(
        persona_id="conv-1",
        store_kind=DOCUMENT_STORE_KIND,
        chunks=[PersonaChunk(id=f"d{i}", text=f"page {i}", created_at=_NOW) for i in range(2)],
    )

    assert store.delete("conv-1") == 2
    assert store.delete("conv-1") == 0
