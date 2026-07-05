"""Unit tests for ``persona.stores.document_store.DocumentStore`` (spec 14 T03).

Tests the conversation-scoped storage discipline (D-14-X-scope-binding-discipline)
via a behavioural in-memory ``Backend`` fake: every call site must pass
``conversation_id`` into the Protocol's ``persona_id`` slot and
``store_kind="document"``. The four typed-store kinds must never appear.

The §6 binary structural guard (criterion #6 — no leak into the four typed
stores) lives in the integration suite at
``packages/core/tests/integration/test_document_store_no_leak.py`` (T04).
This unit suite covers the DocumentStore's own surface.
"""
# ruff: noqa: ANN401

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from persona.schema.documents import (
    DOCUMENT_STORE_KIND,
    DocumentChunk,
    make_document_chunk_id,
)
from persona.stores.backend import Backend
from persona.stores.document_store import DocumentStore

if TYPE_CHECKING:
    from persona.schema.chunks import PersonaChunk

UTC_NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)


class _InMemoryBackend:
    """Behavioural in-memory Backend — stores chunks keyed by (persona_id, store_kind).

    Tracks every call's ``persona_id`` and ``store_kind`` so tests can assert
    the calling-convention discipline: DocumentStore must never pass a typed-
    store ``store_kind`` (``identity``/``self_facts``/``worldview``/``episodic``).
    """

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], list[PersonaChunk]] = {}
        self.call_log: list[tuple[str, str, str]] = []  # (method, persona_id, store_kind)

    def upsert(
        self,
        *,
        persona_id: str,
        store_kind: str,
        chunks: list[PersonaChunk],
    ) -> None:
        self.call_log.append(("upsert", persona_id, store_kind))
        key = (persona_id, store_kind)
        existing = self.store.setdefault(key, [])
        # Replace by id (sloppy but enough for tests).
        existing_ids = {c.id for c in chunks}
        kept = [c for c in existing if c.id not in existing_ids]
        kept.extend(chunks)
        self.store[key] = kept

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,  # noqa: ARG002 — fake doesn't embed
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[PersonaChunk]:
        self.call_log.append(("query", persona_id, store_kind))
        chunks = list(self.store.get((persona_id, store_kind), []))
        if where:
            chunks = [c for c in chunks if all(c.metadata.get(k) == v for k, v in where.items())]
        # Assign a fake distance so DocumentChunk.distance round-trips.
        result = []
        for i, chunk in enumerate(chunks[:top_k]):
            result.append(chunk.model_copy(update={"distance": 0.1 * (i + 1)}))
        return result

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        self.call_log.append(("get_all", persona_id, store_kind))
        return list(self.store.get((persona_id, store_kind), []))

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.call_log.append(("delete_persona", persona_id, store_kind))
        self.store.pop((persona_id, store_kind), None)

    def delete_documents(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],
    ) -> None:
        self.call_log.append(("delete_documents", persona_id, store_kind))
        key = (persona_id, store_kind)
        existing = self.store.get(key, [])
        self.store[key] = [c for c in existing if c.id not in set(ids)]

    # Spec K8 (T1a/T1b) additive Backend surface.
    def set_bands(
        self,
        *,
        persona_id: str,
        store_kind: str,
        bands: dict[str, int],  # noqa: ARG002 — protocol signature; the fake only logs
    ) -> None:
        self.call_log.append(("set_bands", persona_id, store_kind))

    def band_histogram(self, *, persona_id: str, store_kind: str) -> dict[int, int]:
        self.call_log.append(("band_histogram", persona_id, store_kind))
        return {}

    def reinforce(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],  # noqa: ARG002 — protocol signature; the fake only logs
        recalled_at: object,  # noqa: ARG002 — protocol signature
    ) -> None:
        self.call_log.append(("reinforce", persona_id, store_kind))

    def count(self, *, persona_id: str, store_kind: str, include_superseded: bool = False) -> int:
        self.call_log.append(("count", persona_id, store_kind))
        chunks = self.store.get((persona_id, store_kind), [])
        if include_superseded:
            return len(chunks)
        return len(
            [c for c in chunks if c.provenance is None or c.provenance.superseded_by is None]
        )

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        self.call_log.append(("recent", persona_id, store_kind))
        current = [
            c
            for c in self.store.get((persona_id, store_kind), [])
            if c.provenance is None or c.provenance.superseded_by is None
        ]
        current.sort(key=lambda c: (c.created_at, c.id), reverse=True)
        return current[:limit]

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        self.call_log.append(("get_by_logical_ids", persona_id, store_kind))
        wanted = set(logical_ids)
        return [
            c
            for c in self.store.get((persona_id, store_kind), [])
            if c.provenance is not None and c.provenance.logical_id in wanted
        ]


def _make_chunk(conv: str, doc_ref: str, index: int, text: str = "hello") -> DocumentChunk:
    return DocumentChunk(
        id=make_document_chunk_id(conv, doc_ref, index),
        text=text,
        doc_ref=doc_ref,
        format="pdf",
        title=doc_ref,
        page=index + 1,
        created_at=UTC_NOW,
    )


class TestBackendProtocol:
    def test_in_memory_backend_satisfies_protocol(self) -> None:
        # Sanity: the fake satisfies the runtime-checkable Backend Protocol.
        assert isinstance(_InMemoryBackend(), Backend)


class TestCallingConventionDiscipline:
    """D-14-X-scope-binding-discipline + CSA-1 — the load-bearing call."""

    def test_write_passes_conversation_id_into_persona_id_slot(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        chunk = _make_chunk("conv-123", "memo.pdf", 0)
        store.write("conv-123", [chunk])
        # The single upsert call carried conversation_id in the persona_id slot.
        assert backend.call_log == [("upsert", "conv-123", DOCUMENT_STORE_KIND)]

    def test_every_method_uses_document_store_kind(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write("conv", [_make_chunk("conv", "x.pdf", 0)])
        store.query("conv", "hi", top_k=1)
        store.get_all("conv")
        store.delete_document("conv", "x.pdf")
        store.delete("conv")
        for method, _, store_kind in backend.call_log:
            assert store_kind == DOCUMENT_STORE_KIND, (
                f"{method} called with non-document store_kind {store_kind!r}"
            )

    def test_no_call_uses_any_typed_store_kind(self) -> None:
        # The structural intent — DocumentStore never even uses a TypedStore
        # ``store_kind`` value, regardless of operation. The T04 binary guard
        # (integration) widens this to "never invokes typed-store .write()".
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write("conv", [_make_chunk("conv", "x.pdf", 0)])
        store.query("conv", "hi", top_k=1)
        store.get_all("conv")
        store.delete_document("conv", "x.pdf")
        store.delete("conv")
        typed_kinds = {"identity", "self_facts", "worldview", "episodic"}
        for method, _, store_kind in backend.call_log:
            assert store_kind not in typed_kinds, (
                f"{method} used a typed-store kind: {store_kind!r}"
            )


class TestWrite:
    def test_empty_chunks_is_a_no_op(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write("conv", [])
        assert backend.call_log == []

    def test_single_chunk_round_trips_via_storage_conversion(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        chunk = _make_chunk("conv", "memo.pdf", 0, text="hello world")
        store.write("conv", [chunk])
        retrieved = store.get_all("conv")
        assert len(retrieved) == 1
        assert retrieved[0].text == "hello world"
        assert retrieved[0].doc_ref == "memo.pdf"

    def test_multiple_chunks_one_call(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        chunks = [_make_chunk("conv", "memo.pdf", i) for i in range(3)]
        store.write("conv", chunks)
        # Backend.upsert called exactly once with all three chunks.
        upsert_calls = [c for c in backend.call_log if c[0] == "upsert"]
        assert len(upsert_calls) == 1


class TestQuery:
    def test_returns_document_chunks_not_persona_chunks(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write("conv", [_make_chunk("conv", "memo.pdf", 0)])
        result = store.query("conv", "anything", top_k=1)
        assert all(isinstance(c, DocumentChunk) for c in result)

    def test_top_k_is_respected(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write("conv", [_make_chunk("conv", "memo.pdf", i) for i in range(5)])
        result = store.query("conv", "hi", top_k=3)
        assert len(result) == 3

    def test_distance_populated_on_returned_chunks(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write("conv", [_make_chunk("conv", "memo.pdf", 0)])
        result = store.query("conv", "hi", top_k=1)
        assert result[0].distance is not None

    def test_filters_pass_to_backend_where_clause(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write(
            "conv",
            [_make_chunk("conv", "a.pdf", 0), _make_chunk("conv", "b.pdf", 0)],
        )
        result = store.query("conv", "hi", top_k=10, doc_ref="a.pdf")
        assert {c.doc_ref for c in result} == {"a.pdf"}


class TestGetAll:
    def test_returns_all_for_conversation(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        chunks = [_make_chunk("conv", "memo.pdf", i) for i in range(4)]
        store.write("conv", chunks)
        result = store.get_all("conv")
        assert len(result) == 4
        assert all(isinstance(c, DocumentChunk) for c in result)

    def test_scoped_per_conversation_no_bleed(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write("conv-A", [_make_chunk("conv-A", "x.pdf", 0)])
        store.write("conv-B", [_make_chunk("conv-B", "y.pdf", 0)])
        assert len(store.get_all("conv-A")) == 1
        assert len(store.get_all("conv-B")) == 1
        # The two conversations don't see each other's documents — criterion #5.
        a_docs = {c.doc_ref for c in store.get_all("conv-A")}
        b_docs = {c.doc_ref for c in store.get_all("conv-B")}
        assert a_docs == {"x.pdf"}
        assert b_docs == {"y.pdf"}


class TestDelete:
    def test_delete_clears_entire_conversation(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write(
            "conv",
            [
                _make_chunk("conv", "a.pdf", 0),
                _make_chunk("conv", "b.pdf", 0),
            ],
        )
        store.delete("conv")
        assert store.get_all("conv") == []

    def test_delete_is_idempotent(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        # Delete on empty conv must not raise.
        store.delete("conv-never-written")
        store.delete("conv-never-written")  # twice


class TestDeleteDocument:
    def test_removes_only_target_document(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write(
            "conv",
            [
                _make_chunk("conv", "a.pdf", 0),
                _make_chunk("conv", "a.pdf", 1),
                _make_chunk("conv", "b.pdf", 0),
            ],
        )
        store.delete_document("conv", "a.pdf")
        remaining = store.get_all("conv")
        assert {c.doc_ref for c in remaining} == {"b.pdf"}

    def test_removes_all_chunks_of_target_document(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        chunks = [_make_chunk("conv", "report.pdf", i) for i in range(5)]
        store.write("conv", chunks)
        store.delete_document("conv", "report.pdf")
        assert store.get_all("conv") == []

    def test_is_idempotent_for_unknown_doc_ref(self) -> None:
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write("conv", [_make_chunk("conv", "a.pdf", 0)])
        store.delete_document("conv", "no-such-doc.pdf")  # must not raise
        assert len(store.get_all("conv")) == 1

    def test_does_not_touch_other_conversations(self) -> None:
        # Criterion #5 — cross-conversation isolation.
        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        store.write("conv-A", [_make_chunk("conv-A", "x.pdf", 0)])
        store.write("conv-B", [_make_chunk("conv-B", "x.pdf", 0)])
        store.delete_document("conv-A", "x.pdf")
        assert store.get_all("conv-A") == []
        assert len(store.get_all("conv-B")) == 1

    def test_doc_ref_with_delimiter_rejected(self) -> None:
        # The chunk-ID format relies on doc_ref being delimiter-free for the
        # prefix-match. Same validation as make_document_chunk_id.
        import pytest

        backend = _InMemoryBackend()
        store = DocumentStore(backend=backend)
        with pytest.raises(ValueError, match="doc_ref must not contain"):
            store.delete_document("conv", "weird::name")


class TestNoSourcePolicyAxis:
    """D-14-X-no-source-policy-on-documents — the upload IS the provenance."""

    def test_write_signature_takes_no_source_or_force_kwargs(self) -> None:
        # The DocumentStore.write contract intentionally omits the three-source
        # axis (source/force/written_by/reason). Confirm via the function
        # signature itself.
        import inspect

        sig = inspect.signature(DocumentStore.write)
        kwargs = set(sig.parameters) - {"self"}
        # Exactly the two domain inputs — no audit/source policy carriers.
        assert kwargs == {"conversation_id", "chunks"}
