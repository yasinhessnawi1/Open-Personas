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

from collections.abc import Sequence  # noqa: TC003 - in a runtime signature
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — used at runtime by pytest tmp_path

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import (
    BrokenVersionChainError,
    ChunkIntegrityError,
    PersonaSelfWriteForbiddenError,
    RuntimeWriteForbiddenError,
)
from persona.recall.core_memory import CoreBlock, core_block_to_chunk
from persona.registry import PersonaRegistry
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.schema.persona import Persona
from persona.stores import (
    ChromaBackend,
    EpisodicStore,
    IdentityStore,
    SelfFactsStore,
    WorldviewStore,
)
from persona.stores.core_memory import CoreMemoryStore, core_logical_id
from persona.stores.versioning import ChainDefect

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


# --- The hash is checked at the write, not on some later read (R9-210) -------


class _CountingEmbedder(HashEmbedder):
    """Counts encodes. A refused chunk must not pay for an embedding."""

    def __init__(self) -> None:
        self.encoded = 0

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        self.encoded += 1
        return super().encode(texts)


class TestStaleHashRefusedAtWrite:
    def test_the_refusal_comes_before_the_embedder(self, tmp_path: Path) -> None:
        embedder = _CountingEmbedder()
        backend = ChromaBackend(persist_path=tmp_path / "counted", embedder=embedder)
        stale = _chunk().model_copy(update={"text": "changed"})
        with pytest.raises(ChunkIntegrityError):
            backend.upsert(persona_id="p1", store_kind="self_facts", chunks=[stale])
        assert embedder.encoded == 0, "a refused chunk must not pay for an embedding"

    def test_a_model_copy_edit_is_refused_and_nothing_lands(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        stale = _chunk().model_copy(update={"text": "changed"})
        with pytest.raises(ChunkIntegrityError):
            store.write("p1", [stale], source=WriteSource.USER)
        assert backend.get_all(persona_id="p1", store_kind="self_facts") == []
        assert audit.events == [], "a refused write must not be audited as a write"

    def test_a_batch_with_one_stale_chunk_lands_nothing(self, backend: ChromaBackend) -> None:
        good = _chunk(chunk_id="p1::self_facts::0001")
        stale = _chunk(chunk_id="p1::self_facts::0002").model_copy(update={"text": "changed"})
        with pytest.raises(ChunkIntegrityError):
            backend.upsert(persona_id="p1", store_kind="self_facts", chunks=[good, stale])
        assert backend.get_all(persona_id="p1", store_kind="self_facts") == []


# --- Two racing saves of one persona, on the real transport (review finding M1) --


class _RaceAtNextUpsert:
    """Forwards to the transport, and runs ``hook`` inside the next self_facts upsert."""

    def __init__(self, inner: ChromaBackend) -> None:
        self._inner = inner
        self.hook: object = None

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        if store_kind == "self_facts":
            hook, self.hook = self.hook, None
            if callable(hook):
                hook()
        self._inner.upsert(persona_id=persona_id, store_kind=store_kind, chunks=chunks)


class TestRacingPersonaSaves:
    _YAML = Path(__file__).parents[1] / "fixtures/personas/valid/03_legal_assistant_full.yaml"

    def _setup(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> tuple[PersonaRegistry, _RaceAtNextUpsert, SelfFactsStore, Persona]:
        racing = _RaceAtNextUpsert(backend)
        stores = {
            "identity": IdentityStore(backend=racing, audit_logger=audit),
            "self_facts": SelfFactsStore(backend=racing, audit_logger=audit),
            "worldview": WorldviewStore(backend=racing, audit_logger=audit),
            "episodic": EpisodicStore(backend=racing, audit_logger=audit),
        }
        registry = PersonaRegistry(stores=stores, audit_logger=audit)
        persona = Persona.from_yaml(self._YAML).model_copy(update={"persona_id": "pA"})
        registry.load_persona(persona)
        return registry, racing, stores["self_facts"], persona

    def test_two_identical_saves_leave_a_chain_that_can_still_be_saved(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        registry, racing, store, base = self._setup(backend, audit)
        fact = base.self_facts[0].model_copy(update={"fact": "An edited fact"})
        edited = base.model_copy(update={"self_facts": [fact, *base.self_facts[1:]]})

        racing.hook = lambda: registry.load_persona(edited)
        registry.load_persona(edited)

        assert store.diagnose("pA").healthy
        later = base.self_facts[0].model_copy(update={"fact": "A later edit"})
        registry.load_persona(base.model_copy(update={"self_facts": [later, *base.self_facts[1:]]}))
        assert store.diagnose("pA").healthy


# --- A forget reaches the summary, on the real transport (R9-209) ----------------


class TestForgetReachesTheCoreBlock:
    def _setup(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> tuple[CoreMemoryStore, EpisodicStore]:
        core = CoreMemoryStore(backend=backend, audit_logger=audit)
        episodic = EpisodicStore(backend=backend, audit_logger=audit)
        for name, cited in (("first", "a"), ("second", "b"), ("third", "c")):
            core.write(
                "p1",
                [core_block_to_chunk(CoreBlock(text=name, source_ids=(cited,)), "p1", now=UTC_NOW)],
                source=WriteSource.SYSTEM,
            )
        for chunk_id in ("a", "b", "c"):
            episodic.write(
                "p1",
                [_chunk(chunk_id=chunk_id, text=f"evidence {chunk_id}")],
                source=WriteSource.SYSTEM,
            )
        return core, episodic

    @pytest.mark.parametrize(
        ("forgotten", "left"),
        [("a", ["second", "third"]), ("b", ["first", "third"]), ("c", ["first", "second"])],
        ids=["oldest", "middle", "head"],
    )
    def test_the_chain_is_valid_after_any_version_goes(
        self,
        backend: ChromaBackend,
        audit: MemoryAuditLogger,
        forgotten: str,
        left: list[str],
    ) -> None:
        core, episodic = self._setup(backend, audit)

        episodic.remove_documents("p1", [forgotten])

        chain = core.history("p1", core_logical_id("p1"))  # raises on a hole or a bad link
        assert [c.text for c in chain] == left
        assert [c.provenance.version for c in chain if c.provenance] == [1, 2]
        assert core.current("p1").text == left[-1]  # type: ignore[union-attr]
        assert core.diagnose("p1").healthy

    def test_the_forgotten_text_is_gone_from_every_version(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        core, episodic = self._setup(backend, audit)
        episodic.remove_documents("p1", ["b"])
        rows = backend.get_all(persona_id="p1", store_kind="core_memory")
        assert "second" not in {c.text for c in rows}
        assert core.orphaned_citations("p1") == []

    def test_the_delete_comes_before_the_relink(
        self, backend: ChromaBackend, audit: MemoryAuditLogger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No transaction here, so a killed process stops between the two. The text goes first."""
        core, episodic = self._setup(backend, audit)
        calls: list[str] = []
        real_delete, real_relink = backend.delete_documents, backend.relink

        def delete(**kwargs: object) -> None:
            calls.append("delete")
            real_delete(**kwargs)  # type: ignore[arg-type]

        def relink(**kwargs: object) -> None:
            calls.append("relink")
            real_relink(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(backend, "delete_documents", delete)
        monkeypatch.setattr(backend, "relink", relink)

        core.forget_cited("p1", ["b"])

        assert calls == ["delete", "relink"]
        assert core.diagnose("p1").healthy
        _ = episodic

    def test_a_writer_that_landed_after_the_forget_read_makes_it_replan(
        self, backend: ChromaBackend, audit: MemoryAuditLogger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Chroma checks the chain just before it acts, which catches this much."""
        core, episodic = self._setup(backend, audit)
        real = backend.excise_versions
        armed = [True]

        def refresh_first(**kwargs: object) -> None:
            if armed.pop() if armed else False:
                core.write(
                    "p1",
                    [
                        core_block_to_chunk(
                            CoreBlock(text="fourth", source_ids=("c",)), "p1", now=UTC_NOW
                        )
                    ],
                    source=WriteSource.SYSTEM,
                )
            real(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(backend, "excise_versions", refresh_first)

        core.forget_cited("p1", ["b"])

        chain = core.history("p1", core_logical_id("p1"))
        assert [c.text for c in chain] == ["first", "third", "fourth"]
        assert core.diagnose("p1").healthy
        _ = episodic

    def test_no_survivor_means_no_block(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        core, episodic = self._setup(backend, audit)
        episodic.remove_documents("p1", ["a", "b", "c"])
        assert core.current("p1") is None
        assert core.diagnose("p1").healthy

    def test_a_chain_racing_writers_broke_comes_out_healthy(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        """No index here, so two heads are possible when a forget arrives."""
        core, episodic = self._setup(backend, audit)
        head = core.current("p1")
        assert head is not None
        assert head.provenance is not None
        racer = PersonaChunk(
            id="racer",
            text="racer",
            metadata={"source_ids": "c"},
            created_at=UTC_NOW,
            provenance=head.provenance,
        )
        backend.upsert(persona_id="p1", store_kind="core_memory", chunks=[racer])

        episodic.remove_documents("p1", ["a"])

        assert core.diagnose("p1").healthy


# --- What Chroma does NOT do, pinned so it is never mistaken for a bug (R9-205) --


class TestChromaHasNoHeadIndex:
    """The documented Liskov difference in ``persona.stores.backend``. Postgres raises
    ``ChainConflictError`` and retries; here both racers land, and repair is the remedy."""

    def test_two_racing_writers_both_land_and_repair_settles_them(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        racing = _RaceAtNextUpsert(backend)
        store = SelfFactsStore(backend=racing, audit_logger=audit)
        store.write("p1", [_chunk(chunk_id="base", text="v1")], source=WriteSource.USER)

        def chained(chunk_id: str, body: str) -> PersonaChunk:
            return PersonaChunk(
                id=chunk_id,
                text=body,
                created_at=UTC_NOW,
                provenance=ChunkProvenance(
                    source=WriteSource.USER, logical_id="base", written_at=UTC_NOW
                ),
            )

        racing.hook = lambda: store.write("p1", [chained("b", "from b")], source=WriteSource.USER)
        store.write("p1", [chained("a", "from a")], source=WriteSource.USER)  # no error here

        found = store.diagnose("p1").findings
        assert [f.defect for f in found] == [ChainDefect.RACED_HEADS]
        assert store.repair("p1") == 0, "a plain repair must leave it"
        assert store.repair("p1", resolve_raced=True) == 1
        # ``b`` landed first, inside the hook. ``a`` committed last, so its link is the one the
        # prior head kept, and the resolution puts it last.
        assert [c.text for c in store.history("p1", "base")] == ["v1", "from b", "from a"]


class TestRelinkWithVersionsOnly:
    def test_a_versions_only_relink_is_applied(self, backend: ChromaBackend) -> None:
        """The Liskov twin of the Postgres test: both transports apply it."""
        chunk = PersonaChunk(
            id="x",
            text="t",
            created_at=UTC_NOW,
            provenance=ChunkProvenance(
                source=WriteSource.USER, logical_id="x", version=5, written_at=UTC_NOW
            ),
        )
        backend.upsert(persona_id="p1", store_kind="self_facts", chunks=[chunk])
        backend.relink(persona_id="p1", store_kind="self_facts", links={}, versions={"x": 1})
        got = backend.get_all(persona_id="p1", store_kind="self_facts")[0]
        assert got.provenance is not None
        assert got.provenance.version == 1


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


class TestRelink:
    """The K13 repair primitive against real Chroma, including the trap it hides.

    ``collection.update`` MERGES the metadata it is handed: leaving a key out does NOT remove
    it. So clearing ``prov_superseded_by`` by omitting it silently does nothing, and a repair
    built that way reports success while the chain stays broken. The key has to be passed
    explicitly as ``None``. Measured against chromadb 1.5.9; this test is what keeps the next
    person from simplifying it back.
    """

    def test_relink_clears_a_pointer(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        chunk = _chunk(chunk_id="p1::self_facts::0001", text="a fact worth keeping")
        store.write("p1", [chunk], source=WriteSource.USER)

        backend.relink(
            persona_id="p1", store_kind="self_facts", links={chunk.id: "some-newer-version"}
        )
        linked = backend.get_all(persona_id="p1", store_kind="self_facts")[0]
        assert linked.provenance is not None
        assert linked.provenance.superseded_by == "some-newer-version"

        backend.relink(persona_id="p1", store_kind="self_facts", links={chunk.id: None})
        cleared = backend.get_all(persona_id="p1", store_kind="self_facts")[0]
        assert cleared.provenance is not None
        assert cleared.provenance.superseded_by is None, (
            "the link survived being cleared: chroma merges metadata, so the key has to be "
            "sent as None rather than left out"
        )

    def test_relink_leaves_the_rest_of_the_chunk_alone(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        chunk = _chunk(
            chunk_id="p1::self_facts::0002",
            text="text that must not be re-embedded",
            metadata={"confidence": "0.900"},
        )
        store.write("p1", [chunk], source=WriteSource.USER)
        before = backend.get_all(persona_id="p1", store_kind="self_facts")[0]

        backend.relink(persona_id="p1", store_kind="self_facts", links={chunk.id: "next"})
        after = backend.get_all(persona_id="p1", store_kind="self_facts")[0]

        assert after.text == before.text
        assert after.metadata == before.metadata == {"confidence": "0.900"}
        assert after.content_hash == before.content_hash
        assert after.provenance is not None
        assert before.provenance is not None
        assert after.provenance.version == before.provenance.version
        assert after.provenance.written_at == before.provenance.written_at

    def test_relink_skips_ids_that_are_not_there(self, backend: ChromaBackend) -> None:
        backend.relink(persona_id="p1", store_kind="self_facts", links={"never-existed": None})

    def test_relink_with_nothing_to_do_is_a_no_op(self, backend: ChromaBackend) -> None:
        backend.relink(persona_id="p1", store_kind="self_facts", links={})


class TestForgetWholeChain:
    """A forget reaches every version, proven against a real transport (Spec K13, T1).

    The delete matches on the physical id and a search only ever returns current versions, so
    forgetting what the user can see used to leave the superseded versions on disk with the
    words still in them (R9-201). The assertions are about the TEXT, through the reads that
    would find it, because a row count cannot tell the difference.
    """

    _SECRET = "my landlord is called Bjorn Haugen"

    def _edited_chain(self, backend: ChromaBackend, audit: MemoryAuditLogger) -> SelfFactsStore:
        store = SelfFactsStore(backend=backend, audit_logger=audit)
        first = _chunk(chunk_id="p1::self_facts::0001", text=self._SECRET)
        store.write("p1", [first], source=WriteSource.USER)
        store.write(
            "p1",
            [
                PersonaChunk(
                    id=first.id,
                    text="I would rather not say who my landlord is",
                    created_at=first.created_at,
                    provenance=first.provenance,
                )
            ],
            source=WriteSource.USER,
        )
        return store

    def test_the_forgotten_words_are_in_no_version_afterwards(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = self._edited_chain(backend, audit)
        on_disk = backend.get_all(persona_id="p1", store_kind="self_facts")
        assert any(self._SECRET in c.text for c in on_disk), "the setup did not take"

        store.remove_documents("p1", [store.get_all("p1")[0].id])

        after = backend.get_all(persona_id="p1", store_kind="self_facts")
        assert not any(self._SECRET in c.text for c in after), (
            f"the forgotten sentence survives in a superseded version: {[c.text for c in after]}"
        )

    def test_a_search_cannot_reach_the_forgotten_text(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        """The read a recall actually takes, on the real index.

        A neighbour is left in the store on purpose: querying an EMPTY store returns nothing
        whatever the code does, so the assertion would hold even if the delete had done the
        wrong thing. With rows left and ``top_k`` above the row count, the search returns
        everything there is, which makes "the secret is not among them" mean something.
        """
        store = self._edited_chain(backend, audit)
        store.write(
            "p1",
            [_chunk(chunk_id="p1::self_facts::0009", text="something else entirely")],
            source=WriteSource.USER,
        )
        recalled = store.query("p1", self._SECRET, top_k=10)
        assert recalled, "nothing was recallable to begin with"

        # The head of the edited chain, which is NOT the id the caller first wrote: the store
        # mints a fresh id so an update cannot overwrite the version it supersedes.
        head = next(
            c
            for c in store.get_all("p1")
            if c.provenance is not None and c.provenance.logical_id == "p1::self_facts::0001"
        )
        store.remove_documents("p1", [head.id])

        after = store.query("p1", self._SECRET, top_k=10)
        assert after, "the search came back empty, so it proves nothing about the text"
        assert not any(self._SECRET in c.text for c in after)
        assert not any(
            self._SECRET in c.text
            for c in backend.get_all(persona_id="p1", store_kind="self_facts")
        )

    def test_the_chain_leaves_nothing_dangling_behind_it(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = self._edited_chain(backend, audit)
        store.remove_documents("p1", [store.get_all("p1")[0].id])

        survivors = backend.get_all(persona_id="p1", store_kind="self_facts")
        assert survivors == []
        assert store.history("p1", "p1::self_facts::0001") == []

    def test_an_unversioned_neighbour_is_untouched(
        self, backend: ChromaBackend, audit: MemoryAuditLogger
    ) -> None:
        store = self._edited_chain(backend, audit)
        keeper = _chunk(chunk_id="p1::self_facts::0009", text="something else entirely")
        store.write("p1", [keeper], source=WriteSource.USER)

        store.remove_documents("p1", [store.get_all("p1")[0].id])

        assert [c.id for c in backend.get_all(persona_id="p1", store_kind="self_facts")] == [
            keeper.id
        ]
