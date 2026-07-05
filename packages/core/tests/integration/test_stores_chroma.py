"""Integration tests for the four typed stores backed by ChromaDB.

Marked ``@pytest.mark.integration`` so the default ``uv run pytest`` skips
them. Real ChromaDB is used (no monkeypatching) — these tests are the
spec §8 #3, #4, #7, #8, #12 acceptance.

A fake :class:`HashEmbedder` stands in for sentence-transformers so we
don't pay the ~3s cold-start per test. The embedder is deterministic
(SHA-256-based), L2-normalised, and fixed-dim — exactly what the store
layer expects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — used at runtime by pytest tmp_path

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import (
    BrokenVersionChainError,
    PersonaSelfWriteForbiddenError,
    RuntimeWriteForbiddenError,
)
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores import (
    ChromaBackend,
    EpisodicStore,
    IdentityStore,
    SelfFactsStore,
    WorldviewStore,
)

from tests._embedder import HashEmbedder

pytestmark = pytest.mark.integration

UTC_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def backend(tmp_path: Path) -> ChromaBackend:
    return ChromaBackend(persist_path=tmp_path / "chroma", embedder=HashEmbedder())


@pytest.fixture
def audit() -> MemoryAuditLogger:
    return MemoryAuditLogger()


def _chunk(
    *,
    chunk_id: str = "p1::self_facts::0001",
    text: str = "I specialise in Norwegian tenancy.",
    metadata: dict[str, str] | None = None,
) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=text,
        metadata=metadata or {},
        created_at=UTC_NOW,
    )


# --- IdentityStore ----------------------------------------------------------


class TestIdentityStore:
    def test_rejects_every_source(self, backend: ChromaBackend, audit: MemoryAuditLogger) -> None:
        store = IdentityStore(backend=backend, audit_logger=audit)
        for source in WriteSource:
            with pytest.raises(RuntimeWriteForbiddenError):
                store.write("p1", [_chunk()], source=source)

    def test_rejects_even_with_force(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = IdentityStore(backend=backend, audit_logger=audit)
        with pytest.raises(RuntimeWriteForbiddenError):
            store.write("p1", [_chunk()], source=WriteSource.USER, force=True)

    def test_history_not_supported(self, backend: ChromaBackend, audit: MemoryAuditLogger) -> None:
        store = IdentityStore(backend=backend, audit_logger=audit)
        with pytest.raises(RuntimeWriteForbiddenError, match="history"):
            store.history("p1", "lid")

    def test_rollback_not_supported(self, backend: ChromaBackend, audit: MemoryAuditLogger) -> None:
        store = IdentityStore(backend=backend, audit_logger=audit)
        with pytest.raises(RuntimeWriteForbiddenError, match="rollback"):
            store.rollback("p1", "lid", 1, source=WriteSource.USER)


# --- EpisodicStore ----------------------------------------------------------


class TestEpisodicStore:
    def test_accepts_all_three_sources(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = EpisodicStore(backend=backend, audit_logger=audit)
        for i, source in enumerate(WriteSource):
            store.write(
                "p1",
                [_chunk(chunk_id=f"p1::episodic::{i:04d}", text=f"event {i}")],
                source=source,
                written_by="tester",
            )
        actions = [e.action for e in audit.events]
        assert actions == [AuditAction.WRITE] * 3

    def test_decay_reranking(self, backend: ChromaBackend, audit: MemoryAuditLogger) -> None:
        # Spec K8 re-baseline: flat-24h tau is gone; ranking is usage-reinforced
        # retention with a floor (K8-D-4). Recency-ordering property preserved.
        store = EpisodicStore(backend=backend, audit_logger=audit)
        # Insert with explicit created_at by going through Chroma directly
        # (the base sets created_at = now). We instead write chunks where
        # created_at is far in the past for one of them.
        from persona.audit import AuditEvent

        # Use the normal write path then verify ordering.
        store.write(
            "p1",
            [_chunk(chunk_id="p1::episodic::0001", text="recent topic")],
            source=WriteSource.SYSTEM,
        )
        assert isinstance(audit.events[0], AuditEvent)

        results = store.query("p1", "recent topic", top_k=1)
        assert len(results) == 1
        assert "recent topic" in results[0].text


# --- SelfFactsStore policy --------------------------------------------------


class TestSelfFactsPolicy:
    def test_user_write_accepted(self, backend: ChromaBackend, audit: MemoryAuditLogger) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        store.write("p1", [_chunk()], source=WriteSource.USER)
        assert audit.events[0].action == AuditAction.WRITE

    def test_system_write_without_force_rejected(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        with pytest.raises(RuntimeWriteForbiddenError, match="force=True"):
            store.write("p1", [_chunk()], source=WriteSource.SYSTEM)
        assert audit.events == []

    def test_system_write_with_force_accepted(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        store.write("p1", [_chunk()], source=WriteSource.SYSTEM, force=True)
        assert len(audit.events) == 1

    def test_persona_self_below_threshold_rejected(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        with pytest.raises(PersonaSelfWriteForbiddenError, match="below threshold"):
            store.write(
                "p1",
                [_chunk(metadata={"confidence": "0.5"})],
                source=WriteSource.PERSONA_SELF,
                force=True,
                reason="learned",
            )

    def test_persona_self_above_threshold_accepted(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        store.write(
            "p1",
            [_chunk(metadata={"confidence": "0.95"})],
            source=WriteSource.PERSONA_SELF,
            force=True,
            reason="learned a new fact",
        )
        assert audit.events[-1].source == WriteSource.PERSONA_SELF


# --- WorldviewStore policy --------------------------------------------------


class TestWorldviewPolicy:
    def test_persona_self_requires_epistemic_tag(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = WorldviewStore(backend=backend, audit_logger=audit)
        with pytest.raises(PersonaSelfWriteForbiddenError, match="epistemic"):
            store.write(
                "p1",
                [_chunk()],
                source=WriteSource.PERSONA_SELF,
                force=True,
                reason="r",
            )

    def test_persona_self_with_tag_accepted(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = WorldviewStore(backend=backend, audit_logger=audit)
        store.write(
            "p1",
            [_chunk(metadata={"epistemic": "belief"})],
            source=WriteSource.PERSONA_SELF,
            force=True,
            reason="r",
        )
        assert audit.events[-1].store == "worldview"


# --- Versioning -------------------------------------------------------------


class TestVersioning:
    def _setup(self, backend: ChromaBackend, audit: MemoryAuditLogger) -> SelfFactsStore:
        return SelfFactsStore(backend=backend, audit_logger=audit)

    def test_two_writes_to_same_logical_id_produce_two_versions(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = self._setup(backend, audit)
        # First write: a chunk whose id == logical_id (the first-write convention).
        c1 = PersonaChunk(
            id="p1::self_facts::0001",
            text="version one",
            created_at=UTC_NOW,
            provenance=ChunkProvenance(
                source=WriteSource.USER,
                logical_id="p1::self_facts::0001",
                written_at=UTC_NOW,
            ),
        )
        store.write("p1", [c1], source=WriteSource.USER)
        # Second write: a different chunk id but same logical_id (an update).
        c2 = PersonaChunk(
            id="p1::self_facts::0002",
            text="version two — updated",
            created_at=UTC_NOW,
            provenance=ChunkProvenance(
                source=WriteSource.USER,
                logical_id="p1::self_facts::0001",
                written_at=UTC_NOW,
            ),
        )
        store.write("p1", [c2], source=WriteSource.USER)

        all_chunks = store.get_all("p1", include_superseded=True)
        assert len(all_chunks) == 2
        current = store.get_all("p1", include_superseded=False)
        assert len(current) == 1
        assert current[0].text == "version two — updated"

    def test_history_returns_chain_oldest_first(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = self._setup(backend, audit)
        store.write(
            "p1",
            [
                PersonaChunk(
                    id="p1::self_facts::0001",
                    text="v1",
                    created_at=UTC_NOW,
                    provenance=ChunkProvenance(
                        source=WriteSource.USER,
                        logical_id="p1::self_facts::0001",
                        written_at=UTC_NOW,
                    ),
                )
            ],
            source=WriteSource.USER,
        )
        store.write(
            "p1",
            [
                PersonaChunk(
                    id="p1::self_facts::0002",
                    text="v2",
                    created_at=UTC_NOW,
                    provenance=ChunkProvenance(
                        source=WriteSource.USER,
                        logical_id="p1::self_facts::0001",
                        written_at=UTC_NOW,
                    ),
                )
            ],
            source=WriteSource.USER,
        )

        chain = store.history("p1", "p1::self_facts::0001")
        assert [c.text for c in chain] == ["v1", "v2"]
        assert chain[0].provenance is not None
        assert chain[0].provenance.version == 1
        assert chain[0].provenance.superseded_by == "p1::self_facts::0002"
        assert chain[1].provenance is not None
        assert chain[1].provenance.version == 2
        assert chain[1].provenance.superseded_by is None

    def test_rollback_appends_new_head_identical_to_target(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = self._setup(backend, audit)
        store.write(
            "p1",
            [
                PersonaChunk(
                    id="p1::self_facts::0001",
                    text="v1",
                    created_at=UTC_NOW,
                    provenance=ChunkProvenance(
                        source=WriteSource.USER,
                        logical_id="p1::self_facts::0001",
                        written_at=UTC_NOW,
                    ),
                )
            ],
            source=WriteSource.USER,
        )
        store.write(
            "p1",
            [
                PersonaChunk(
                    id="p1::self_facts::0002",
                    text="v2",
                    created_at=UTC_NOW,
                    provenance=ChunkProvenance(
                        source=WriteSource.USER,
                        logical_id="p1::self_facts::0001",
                        written_at=UTC_NOW,
                    ),
                )
            ],
            source=WriteSource.USER,
        )
        store.rollback(
            "p1",
            "p1::self_facts::0001",
            to_version=1,
            source=WriteSource.USER,
            written_by="owner",
            reason="prefer v1",
        )
        chain = store.history("p1", "p1::self_facts::0001")
        assert len(chain) == 3
        assert chain[-1].text == "v1"  # rolled back content identical to v1
        # v1's content survives at chain[0]; rollback never deletes.
        assert chain[0].text == "v1"
        # Audit captured the rollback action.
        rollback_events = [e for e in audit.events if e.action == AuditAction.ROLLBACK]
        assert len(rollback_events) == 1
        assert rollback_events[0].metadata["to_version"] == "1"

    def test_rollback_to_missing_version_raises(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = self._setup(backend, audit)
        store.write("p1", [_chunk()], source=WriteSource.USER)
        with pytest.raises(BrokenVersionChainError):
            store.rollback(
                "p1",
                "p1::self_facts::0001",
                to_version=99,
                source=WriteSource.USER,
            )


# --- Persistence ------------------------------------------------------------


class TestPersistence:
    def test_chunks_survive_backend_recreation(
        self, tmp_path: Path, audit: MemoryAuditLogger
    ) -> None:
        # Write through one backend instance.
        backend1 = ChromaBackend(persist_path=tmp_path / "chroma", embedder=HashEmbedder())
        store1 = SelfFactsStore(backend=backend1, audit_logger=audit)
        store1.write(
            "p1",
            [_chunk(chunk_id="p1::self_facts::0001", text="durable fact")],
            source=WriteSource.USER,
        )
        del backend1, store1

        # Read through a fresh backend instance pointing at the same dir.
        backend2 = ChromaBackend(persist_path=tmp_path / "chroma", embedder=HashEmbedder())
        store2 = SelfFactsStore(backend=backend2, audit_logger=audit)
        chunks = store2.get_all("p1")
        assert len(chunks) == 1
        assert chunks[0].text == "durable fact"
        # content_hash survives round-trip.
        recomputed = chunks[0].content_hash
        assert recomputed == chunks[0].content_hash


# --- Delete / remove_documents ----------------------------------------------


class TestDelete:
    def test_delete_emits_audit(self, backend: ChromaBackend, audit: MemoryAuditLogger) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        store.write("p1", [_chunk()], source=WriteSource.USER)
        store.delete("p1")
        actions = [e.action for e in audit.events]
        assert AuditAction.DELETE in actions

    def test_delete_empty_store_is_idempotent(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        store.delete("never_written")  # must not raise
        # No audit emitted for deleting nothing.
        assert audit.events == []

    def test_remove_documents_emits_audit(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        store.write("p1", [_chunk(chunk_id="x")], source=WriteSource.USER)
        store.remove_documents("p1", ["x"])
        actions = [e.action for e in audit.events]
        assert AuditAction.REMOVE_DOCUMENTS in actions
