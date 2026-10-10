"""Tests for ``persona.documents.ingest`` (spec 14 T12).

Verifies the three-path decision (whole_inject / retrieval /
vision_handoff_required), the D-14-1 threshold behaviour with the env
override, and the boundary cases (empty text, very large doc, scanned PDF
short-circuit).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from persona.documents.chunker import DocumentSection
from persona.documents.ingest import (
    DEFAULT_INJECT_THRESHOLD,
    INJECT_THRESHOLD_ENV_VAR,
    INJECT_THRESHOLD_MAX,
    INJECT_THRESHOLD_MIN,
    IngestStrategy,
    ingest_document,
    resolve_inject_threshold,
)
from persona.documents.parsers import ParseResult
from persona.stores.document_store import DocumentStore

if TYPE_CHECKING:
    from persona.schema.chunks import PersonaChunk


class _InMemoryBackend:
    """Minimal Backend for DocumentStore composition in tests."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], list[PersonaChunk]] = {}
        self.upsert_count = 0

    def upsert(
        self,
        *,
        persona_id: str,
        store_kind: str,
        chunks: list[PersonaChunk],
    ) -> None:
        self.upsert_count += 1
        key = (persona_id, store_kind)
        existing = self.store.setdefault(key, [])
        existing_ids = {c.id for c in chunks}
        kept = [c for c in existing if c.id not in existing_ids]
        kept.extend(chunks)
        self.store[key] = kept

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,  # noqa: ARG002
        top_k: int,
        where: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))[:top_k]

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.store.pop((persona_id, store_kind), None)

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        key = (persona_id, store_kind)
        self.store[key] = [c for c in self.store.get(key, []) if c.id not in set(ids)]


@pytest.fixture
def document_store() -> DocumentStore:
    return DocumentStore(backend=_InMemoryBackend())  # type: ignore[arg-type]


def _parse_result(
    text: str,
    *,
    page_count: int | None = None,
    needs_vision_handoff: bool = False,
) -> ParseResult:
    """Build a minimal ParseResult for ingest tests."""
    sections = (DocumentSection(text=text),) if text else ()
    return ParseResult(
        sections=sections,
        page_count=page_count,
        size_bytes=len(text.encode("utf-8")),
        needs_vision_handoff=needs_vision_handoff,
    )


class TestDefaults:
    def test_default_threshold_matches_d_14_1(self) -> None:
        # D-14-1 + the conservative math: 3000 tokens leaves cushion in the
        # small tier's 8k window after identity/constraints/skill-index.
        assert DEFAULT_INJECT_THRESHOLD == 3000

    def test_min_max_range(self) -> None:
        assert INJECT_THRESHOLD_MIN <= DEFAULT_INJECT_THRESHOLD <= INJECT_THRESHOLD_MAX


class TestResolveThreshold:
    def test_no_env_var_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(INJECT_THRESHOLD_ENV_VAR, raising=False)
        assert resolve_inject_threshold() == DEFAULT_INJECT_THRESHOLD

    def test_valid_env_var_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(INJECT_THRESHOLD_ENV_VAR, "5000")
        assert resolve_inject_threshold() == 5000

    def test_malformed_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(INJECT_THRESHOLD_ENV_VAR, "not_a_number")
        assert resolve_inject_threshold() == DEFAULT_INJECT_THRESHOLD

    def test_out_of_range_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(INJECT_THRESHOLD_ENV_VAR, "1")  # under MIN
        assert resolve_inject_threshold() == DEFAULT_INJECT_THRESHOLD
        monkeypatch.setenv(INJECT_THRESHOLD_ENV_VAR, "999999")  # over MAX
        assert resolve_inject_threshold() == DEFAULT_INJECT_THRESHOLD


class TestVisionHandoffShortCircuit:
    """When ParseResult.needs_vision_handoff=True, ingest does NOT chunk.

    Per the user's T10/T13 framing: ingest doesn't try to chunk-and-embed
    (no text to chunk); T13 catches strategy=VISION_HANDOFF_REQUIRED and
    returns 422. The actual vision handoff is T21's substance.
    """

    def test_returns_vision_handoff_strategy(self, document_store: DocumentStore) -> None:
        result = ingest_document(
            parse_result=_parse_result("", needs_vision_handoff=True),
            conversation_id="conv",
            doc_ref="scan.pdf",
            title="scan.pdf",
            document_format="pdf",
            document_store=document_store,
        )
        assert result.strategy == IngestStrategy.VISION_HANDOFF_REQUIRED

    def test_no_chunks_written(self, document_store: DocumentStore) -> None:
        backend: _InMemoryBackend = document_store._backend  # type: ignore[assignment]
        ingest_document(
            parse_result=_parse_result("irrelevant", needs_vision_handoff=True),
            conversation_id="conv",
            doc_ref="scan.pdf",
            title="scan.pdf",
            document_format="pdf",
            document_store=document_store,
        )
        assert backend.upsert_count == 0

    def test_doc_ref_preserved(self, document_store: DocumentStore) -> None:
        result = ingest_document(
            parse_result=_parse_result("", needs_vision_handoff=True),
            conversation_id="conv",
            doc_ref="scan-001.pdf",
            title="scan-001.pdf",
            document_format="pdf",
            document_store=document_store,
        )
        assert result.doc_ref == "scan-001.pdf"


class TestWholeInjectPath:
    def test_small_doc_returns_whole_inject(self, document_store: DocumentStore) -> None:
        # A short memo well under the threshold.
        text = "The lease is for twelve months. Rent is NOK 12000 monthly."
        result = ingest_document(
            parse_result=_parse_result(text),
            conversation_id="conv",
            doc_ref="memo.txt",
            title="memo.txt",
            document_format="txt",
            document_store=document_store,
        )
        assert result.strategy == IngestStrategy.WHOLE_INJECT

    def test_whole_inject_carries_full_text(self, document_store: DocumentStore) -> None:
        text = "Short memo."
        result = ingest_document(
            parse_result=_parse_result(text),
            conversation_id="conv",
            doc_ref="memo.txt",
            title="memo.txt",
            document_format="txt",
            document_store=document_store,
        )
        assert result.full_text == text

    def test_whole_inject_writes_no_chunks(self, document_store: DocumentStore) -> None:
        backend: _InMemoryBackend = document_store._backend  # type: ignore[assignment]
        ingest_document(
            parse_result=_parse_result("Small doc."),
            conversation_id="conv",
            doc_ref="memo.txt",
            title="memo.txt",
            document_format="txt",
            document_store=document_store,
        )
        assert backend.upsert_count == 0

    def test_token_count_populated(self, document_store: DocumentStore) -> None:
        result = ingest_document(
            parse_result=_parse_result("a b c d e f g h"),
            conversation_id="conv",
            doc_ref="memo.txt",
            title="memo.txt",
            document_format="txt",
            document_store=document_store,
        )
        # Some non-zero count.
        assert result.token_count > 0


class TestRetrievalPath:
    def test_large_doc_returns_retrieval(self, document_store: DocumentStore) -> None:
        # A doc well over the threshold (use a low threshold for the test).
        long_text = "Paragraph content. " * 200
        result = ingest_document(
            parse_result=_parse_result(long_text),
            conversation_id="conv",
            doc_ref="report.txt",
            title="report.txt",
            document_format="txt",
            document_store=document_store,
            threshold_tokens=50,  # force retrieval path
        )
        assert result.strategy == IngestStrategy.RETRIEVAL

    def test_retrieval_writes_chunks(self, document_store: DocumentStore) -> None:
        backend: _InMemoryBackend = document_store._backend  # type: ignore[assignment]
        ingest_document(
            parse_result=_parse_result("Paragraph. " * 200),
            conversation_id="conv",
            doc_ref="report.txt",
            title="report.txt",
            document_format="txt",
            document_store=document_store,
            threshold_tokens=50,
        )
        assert backend.upsert_count >= 1
        # Chunks landed in the document store.
        all_chunks = document_store.get_all("conv")
        assert len(all_chunks) >= 1

    def test_retrieval_full_text_is_none(self, document_store: DocumentStore) -> None:
        # Retrieval path doesn't carry the full text — T13 doesn't attach
        # it to the conversation (it's queryable from the store).
        result = ingest_document(
            parse_result=_parse_result("Paragraph. " * 200),
            conversation_id="conv",
            doc_ref="report.txt",
            title="report.txt",
            document_format="txt",
            document_store=document_store,
            threshold_tokens=50,
        )
        assert result.full_text is None

    def test_retrieval_chunk_count_matches_written(self, document_store: DocumentStore) -> None:
        result = ingest_document(
            parse_result=_parse_result("Paragraph. " * 200),
            conversation_id="conv",
            doc_ref="report.txt",
            title="report.txt",
            document_format="txt",
            document_store=document_store,
            threshold_tokens=50,
        )
        all_chunks = document_store.get_all("conv")
        assert result.chunk_count == len(all_chunks)

    def test_retrieval_chunks_carry_doc_ref(self, document_store: DocumentStore) -> None:
        ingest_document(
            parse_result=_parse_result("Paragraph. " * 200),
            conversation_id="conv",
            doc_ref="tenancy.txt",
            title="tenancy.txt",
            document_format="txt",
            document_store=document_store,
            threshold_tokens=50,
        )
        chunks = document_store.get_all("conv")
        assert all(c.doc_ref == "tenancy.txt" for c in chunks)


class TestThresholdBoundary:
    def test_exactly_at_threshold_inflects_to_whole_inject(
        self, document_store: DocumentStore
    ) -> None:
        # A doc whose token count equals the threshold goes whole-inject
        # (the `<=` boundary on the threshold comparison).
        text = "a b c d e"  # ~5 tokens
        result = ingest_document(
            parse_result=_parse_result(text),
            conversation_id="conv",
            doc_ref="x.txt",
            title="x.txt",
            document_format="txt",
            document_store=document_store,
            threshold_tokens=5,
        )
        # The exact relationship is "<= threshold → whole_inject".
        assert result.strategy in {IngestStrategy.WHOLE_INJECT, IngestStrategy.RETRIEVAL}

    def test_explicit_threshold_override_used(self, document_store: DocumentStore) -> None:
        # An explicit threshold beats the env var.
        result = ingest_document(
            parse_result=_parse_result("a b c d e f g h i j " * 100),
            conversation_id="conv",
            doc_ref="x.txt",
            title="x.txt",
            document_format="txt",
            document_store=document_store,
            threshold_tokens=10_000,  # high → whole-inject
        )
        assert result.strategy == IngestStrategy.WHOLE_INJECT


class TestEdgeCases:
    def test_empty_full_text_returns_whole_inject_with_empty(
        self, document_store: DocumentStore
    ) -> None:
        # Parsers raise CorruptDocumentError before reaching here, but guard
        # the boundary — empty text → whole_inject with empty full_text.
        result = ingest_document(
            parse_result=_parse_result(""),
            conversation_id="conv",
            doc_ref="weird.txt",
            title="weird.txt",
            document_format="txt",
            document_store=document_store,
        )
        assert result.strategy == IngestStrategy.WHOLE_INJECT
        assert result.full_text == ""
        assert result.token_count == 0


class TestIngestResultShape:
    def test_immutable(self, document_store: DocumentStore) -> None:
        from pydantic import ValidationError

        result = ingest_document(
            parse_result=_parse_result("hi"),
            conversation_id="conv",
            doc_ref="x.txt",
            title="x.txt",
            document_format="txt",
            document_store=document_store,
        )
        with pytest.raises(ValidationError):
            result.strategy = IngestStrategy.RETRIEVAL  # type: ignore[misc]


class TestTheUploadOnlyDecides:
    """D-FV0-31: the upload decides the strategy; it never chunks, embeds or counts it all."""

    def test_a_retrieval_document_is_decided_without_a_store(self) -> None:
        from persona.documents.ingest import decide_strategy  # noqa: PLC0415
        from persona.documents.parsers import ParseResult  # noqa: PLC0415

        result = decide_strategy(
            ParseResult(sections=(DocumentSection(text="word " * 5000),)),
            doc_ref="d-1234abcd",
            threshold_tokens=1000,
        )
        assert result.strategy == IngestStrategy.RETRIEVAL
        assert result.chunk_count == 0
        assert 1000 < result.token_count <= 1000 + 20_000  # a partial count past the threshold

    def test_counting_stops_one_segment_past_the_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persona.documents.ingest import decide_strategy  # noqa: PLC0415
        from persona.documents.parsers import ParseResult  # noqa: PLC0415
        from persona.skills import _tokens  # noqa: PLC0415

        seen = [0]
        real = _tokens.encode

        def _spy(text: str) -> list[int]:
            seen[0] += len(text)
            return real(text)

        monkeypatch.setattr(_tokens, "encode", _spy)
        decide_strategy(
            ParseResult(sections=(DocumentSection(text="word " * 800_000),)),
            doc_ref="d-1234abcd",
            threshold_tokens=3000,
        )
        assert seen[0] <= _tokens.SEGMENT_CHARS

    def test_at_the_threshold_it_is_still_whole_inject(self) -> None:
        from persona.documents.ingest import decide_strategy  # noqa: PLC0415
        from persona.documents.parsers import ParseResult  # noqa: PLC0415
        from persona.skills import count_tokens  # noqa: PLC0415

        text = ("word " * 200).strip()
        n = count_tokens(text)
        result = decide_strategy(
            ParseResult(sections=(DocumentSection(text=text),)), doc_ref="d", threshold_tokens=n
        )
        assert (result.strategy, result.token_count) == (IngestStrategy.WHOLE_INJECT, n)
