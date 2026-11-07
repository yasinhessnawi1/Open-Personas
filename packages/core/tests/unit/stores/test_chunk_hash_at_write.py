"""A chunk whose hash does not match its text is refused when written, not found when read (R9-210).

``PersonaChunk.model_copy`` does not re-run validation, so ``c.model_copy(update={"text": ...})``
keeps the hash of the text it no longer holds. Both transports used to persist that chunk happily,
and the sha256 tamper check then fired on the NEXT READ: from a call site that did nothing wrong,
about a row written long ago, with nothing left to say who wrote it. The check was on the wrong
side of the write.

These run against the real ``PostgresBackend.upsert`` over a stub engine, so the default suite
pins the transport itself with no database. The stub records whether anything reached it, because
the promise is "nothing landed and nothing was paid for", not only "something raised". The Chroma
transport is pinned against a real Chroma in ``tests/integration/test_stores_chroma.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.errors import ChunkIntegrityError, PersonaError
from persona.schema.chunks import (
    ChunkProvenance,
    PersonaChunk,
    WriteSource,
    verify_content_hash,
)
from persona.stores.postgres import EMBEDDING_DIM, PostgresBackend
from persona.stores.self_facts import SelfFactsStore

_NOW = datetime(2026, 11, 4, 9, 0, tzinfo=UTC)


def _chunk(text: str = "the original text", chunk_id: str = "p1::self_facts::0001") -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=text,
        metadata={"confidence": "0.9"},
        created_at=_NOW,
        provenance=ChunkProvenance(source=WriteSource.USER, logical_id=chunk_id, written_at=_NOW),
    )


class _StubEngine:
    """Counts transactions. A refused chunk must never reach one."""

    def __init__(self) -> None:
        self.begun = 0

    def begin(self) -> Any:  # noqa: ANN401 - SQLAlchemy's context manager shape
        self.begun += 1
        msg = "a refused chunk reached the database"
        raise AssertionError(msg)


class _CountingEmbedder:
    """Counts encodes. A refused chunk must not pay for an embedding."""

    model_name = "counting"
    dimension = EMBEDDING_DIM

    def __init__(self) -> None:
        self.encoded = 0

    def encode(self, texts: Any) -> list[list[float]]:  # noqa: ANN401 - Embedder protocol
        self.encoded += 1
        return [[0.0] * EMBEDDING_DIM for _ in texts]


def _backend() -> tuple[PostgresBackend, _StubEngine, _CountingEmbedder]:
    engine = _StubEngine()
    embedder = _CountingEmbedder()
    backend = PostgresBackend(engine=engine, embedder=embedder)  # type: ignore[arg-type]
    return backend, engine, embedder


def test_a_chunk_edited_with_model_copy_keeps_the_old_hash() -> None:
    """The defect, stated as a test: this is the shape everything below refuses."""
    original = _chunk()
    edited = original.model_copy(update={"text": "changed"})
    assert edited.content_hash == original.content_hash


def test_verify_accepts_a_chunk_built_normally() -> None:
    verify_content_hash(_chunk())  # must not raise


def test_verify_accepts_a_provenance_only_copy() -> None:
    """Provenance is outside the hash, and the versioned write copies chunks exactly this way."""
    chunk = _chunk()
    assert chunk.provenance is not None
    relinked = chunk.model_copy(
        update={"provenance": chunk.provenance.model_copy(update={"superseded_by": "next"})}
    )
    verify_content_hash(relinked)  # must not raise


@pytest.mark.parametrize(
    "update",
    [{"text": "changed"}, {"metadata": {"confidence": "0.1"}}, {"content_hash": ""}],
    ids=["text", "metadata", "emptied_hash"],
)
def test_verify_refuses_a_stale_chunk(update: dict[str, Any]) -> None:
    stale = _chunk().model_copy(update=update)
    with pytest.raises(ChunkIntegrityError):
        verify_content_hash(stale)


def test_the_refusal_is_a_domain_error_that_names_the_chunk_and_not_its_text() -> None:
    """A log line has to find the row without copying a person's memory into the log."""
    stale = _chunk(text="a private thing").model_copy(update={"text": "a changed private thing"})
    with pytest.raises(ChunkIntegrityError) as caught:
        verify_content_hash(stale)
    assert isinstance(caught.value, PersonaError)
    assert stale.id in str(caught.value)
    assert "private" not in str(caught.value)


def test_postgres_refuses_before_embedding_or_touching_the_database() -> None:
    backend, engine, embedder = _backend()
    stale = _chunk().model_copy(update={"text": "changed"})
    with pytest.raises(ChunkIntegrityError):
        backend.upsert(persona_id="p1", store_kind="self_facts", chunks=[stale])
    assert embedder.encoded == 0, "a refused chunk must not pay for an embedding"
    assert engine.begun == 0, "a refused chunk must not open a transaction"


def test_postgres_refuses_the_whole_batch_when_one_chunk_is_stale() -> None:
    """One call is one transaction: a batch that lands in part is the torn write K13 removed."""
    backend, engine, embedder = _backend()
    good = _chunk(chunk_id="p1::self_facts::0001")
    stale = _chunk(chunk_id="p1::self_facts::0002").model_copy(update={"text": "changed"})
    with pytest.raises(ChunkIntegrityError):
        backend.upsert(persona_id="p1", store_kind="self_facts", chunks=[good, stale])
    assert embedder.encoded == 0
    assert engine.begun == 0


def test_the_store_write_refuses_it_and_audits_nothing() -> None:
    """The typed store's real path: refused at the transport, so no audit claims a write."""
    backend, _, _ = _backend()
    audit = MemoryAuditLogger()
    store = SelfFactsStore(backend=backend, audit_logger=audit)
    # No read is needed to reach the refusal: a stale chunk with no chain is a first write.
    backend.get_by_logical_ids = lambda **_: []  # type: ignore[method-assign]
    stale = _chunk().model_copy(update={"text": "changed"})
    with pytest.raises(ChunkIntegrityError):
        store.write("p1", [stale], source=WriteSource.USER)
    assert audit.events == []
