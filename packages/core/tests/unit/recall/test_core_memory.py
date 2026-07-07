"""The core-memory block — builder, provenance, refresh, read (Spec K9, T7; K9-D-10).

Pins: the block is built from originals (gists rejected), token-budgeted, cites its sources in
metadata (never replaces), the refresh appends a version under a stable logical id, the read
side reconstructs it, and the store's ``current`` returns the head. Refresh is async/background
by construction; the read side does no summarisation (acceptance-7).
"""

from __future__ import annotations

import pytest
from persona.audit import MemoryAuditLogger
from persona.recall.core_memory import (
    CORE_SOURCE_IDS_KEY,
    CoreBlock,
    build_core_block,
    core_block_from_chunk,
    core_block_to_chunk,
    read_core_block,
    refresh_core_block,
)
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.core_memory import CORE_MEMORY_KIND, CoreMemoryStore, core_logical_id
from persona.stores.summarizer import StubSummarizer, SummarizerError

from tests.unit.recall._fixtures import NOW, chunk, gist


class _FakeStore:
    """A duck-typed CoreMemoryWriter — append-only (latest write is the current head)."""

    def __init__(self) -> None:
        self._head: dict[str, PersonaChunk] = {}
        self.writes = 0

    def write(self, persona_id: str, chunks: list[PersonaChunk], **_kwargs: object) -> None:
        self.writes += 1
        self._head[persona_id] = chunks[-1]

    def current(self, persona_id: str) -> PersonaChunk | None:
        return self._head.get(persona_id)


# ---- build_core_block -----------------------------------------------------


@pytest.mark.asyncio
async def test_build_summarises_the_sources_and_cites_them() -> None:
    sources = [chunk("c0", text="Yasin moved to Oslo in 2023."), chunk("c1", text="He likes tea.")]
    block = await build_core_block(summarizer=StubSummarizer(), sources=sources, target_tokens=200)
    assert block is not None
    assert block.text  # non-empty summary
    assert block.source_ids == ("c0", "c1")  # cites its sources (provenance §0)


@pytest.mark.asyncio
async def test_build_with_no_sources_returns_none() -> None:
    block = await build_core_block(summarizer=StubSummarizer(), sources=[], target_tokens=200)
    assert block is None


@pytest.mark.asyncio
async def test_build_rejects_a_gist_source_from_originals_only() -> None:
    # assemble_summarizer_input refuses gists → never summary-of-summary (§0).
    with pytest.raises(SummarizerError):
        await build_core_block(
            summarizer=StubSummarizer(),
            sources=[gist("g", members=("m0", "m1"))],
            target_tokens=200,
        )


# ---- chunk round-trip -----------------------------------------------------


def test_to_chunk_uses_the_stable_logical_id_and_cites_sources() -> None:
    block = CoreBlock(text="a compact summary", source_ids=("c0", "c1"))
    row = core_block_to_chunk(block, "persona-1", now=NOW)
    assert row.text == "a compact summary"
    assert row.metadata[CORE_SOURCE_IDS_KEY] == "c0,c1"
    assert row.provenance is not None
    assert row.provenance.logical_id == core_logical_id("persona-1")  # append-only chain
    assert CORE_MEMORY_KIND in row.id


def test_from_chunk_reconstructs_the_block() -> None:
    block = CoreBlock(text="s", source_ids=("c0", "c1"))
    reconstructed = core_block_from_chunk(core_block_to_chunk(block, "p", now=NOW))
    assert reconstructed == block


def test_from_chunk_with_no_sources_is_empty_tuple() -> None:
    row = PersonaChunk(id="p::core_memory::x", text="s", metadata={}, created_at=NOW)
    assert core_block_from_chunk(row).source_ids == ()


# ---- refresh (background) + read (turn path) ------------------------------


@pytest.mark.asyncio
async def test_refresh_builds_and_appends_a_version() -> None:
    store = _FakeStore()
    block = await refresh_core_block(
        "p",
        summarizer=StubSummarizer(),
        sources=[chunk("c0", text="a fact")],
        store=store,
        target_tokens=200,
        now=NOW,
    )
    assert block is not None
    assert store.writes == 1
    assert read_core_block(store, "p") == block  # the read side sees the refreshed block


@pytest.mark.asyncio
async def test_refresh_with_no_sources_writes_nothing() -> None:
    store = _FakeStore()
    block = await refresh_core_block(
        "p", summarizer=StubSummarizer(), sources=[], store=store, target_tokens=200, now=NOW
    )
    assert block is None
    assert store.writes == 0  # the prior block stays intact


def test_read_before_any_refresh_is_none() -> None:
    assert read_core_block(_FakeStore(), "p") is None


# ---- CoreMemoryStore.current ----------------------------------------------


class _FakeBackend:
    def __init__(self, rows: list[PersonaChunk]) -> None:
        self._rows = rows

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:  # noqa: ARG002
        return list(self._rows)


def _versioned(cid: str, *, version: int, superseded_by: str | None) -> PersonaChunk:
    return PersonaChunk(
        id=cid,
        text=f"v{version}",
        metadata={},
        created_at=NOW,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id="p::core",
            version=version,
            superseded_by=superseded_by,
            written_at=NOW,
        ),
    )


def test_current_returns_the_unsuperseded_head() -> None:
    old = _versioned("v1", version=1, superseded_by="v2")
    head = _versioned("v2", version=2, superseded_by=None)
    store = CoreMemoryStore(backend=_FakeBackend([old, head]), audit_logger=MemoryAuditLogger())
    assert store.current("p") is not None
    assert store.current("p").id == "v2"  # type: ignore[union-attr]


def test_current_is_none_when_empty() -> None:
    store = CoreMemoryStore(backend=_FakeBackend([]), audit_logger=MemoryAuditLogger())
    assert store.current("p") is None
