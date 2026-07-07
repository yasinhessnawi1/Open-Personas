"""The core-memory block — builder, provenance, and background refresh (Spec K9, T7; K9-D-10).

A compact, always-in-context **user+persona summary** for cross-year coherence (MemGPT/Letta
2310.08560). Built by K8's background summarizer (the :class:`~persona.stores.summarizer.Summarizer`
Protocol, K8-D-10) over the persona's raw memory — **from originals, never summary-of-summary**
(``assemble_summarizer_input`` rejects gists) — token-budgeted, and **derived-with-provenance**:
the block **cites** the source chunk ids in its metadata, it never replaces them (§0). It is
**refreshed only in the background** (K8's engine cadence — never the turn path; acceptance-7),
each refresh appending a new version via :class:`~persona.stores.core_memory.CoreMemoryStore`.

The turn path only **reads** the current block (:func:`read_core_block`) and injects it always-in-
context; an absent block renders nothing (byte-identical — K9-D-11). Building/refresh is async
(the summarizer is async, K8-D-10-AMENDED) and stays off the read path by construction.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.core_memory import CORE_MEMORY_KIND, core_logical_id
from persona.stores.summarizer import assemble_summarizer_input

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from persona.stores.summarizer import Summarizer

__all__ = [
    "CoreBlock",
    "CoreMemoryWriter",
    "build_core_block",
    "core_block_from_chunk",
    "core_block_to_chunk",
    "read_core_block",
    "refresh_core_block",
]

#: Metadata key under which the block cites its source chunk ids (provenance — §0).
CORE_SOURCE_IDS_KEY = "source_ids"


class CoreBlock(BaseModel):
    """The rendered core-memory block + the sources it was derived from (K9-D-10).

    Attributes:
        text: The compact user+persona summary (token-budgeted).
        source_ids: The raw chunk ids the summary was built from — the provenance the
            block **cites, never replaces** (§0). Empty is disallowed at build time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    source_ids: tuple[str, ...]


class CoreMemoryWriter(Protocol):
    """The write side the refresh needs — :class:`CoreMemoryStore` satisfies it structurally."""

    def write(
        self,
        persona_id: str,
        chunks: list[PersonaChunk],
        *,
        source: WriteSource = ...,
        written_by: str | None = ...,
        reason: str | None = ...,
        force: bool = ...,
    ) -> None: ...

    def current(self, persona_id: str) -> PersonaChunk | None: ...


async def build_core_block(
    *,
    summarizer: Summarizer,
    sources: Sequence[PersonaChunk],
    target_tokens: int,
) -> CoreBlock | None:
    """Summarise the persona's raw memory into the core block (K9-D-10) — pure over its deps.

    ``sources`` are raw chunks (the composition selects them — e.g. identity + the most
    important/recent memory); they are assembled **from originals** (gists rejected by
    ``assemble_summarizer_input``) and summarised to ``target_tokens``. Returns ``None`` when
    there is nothing to summarise (no sources) — the caller then leaves the prior block intact.

    Args:
        summarizer: K8's async summarization port (the stub in dev/tests; P7's model later).
        sources: The raw chunks to summarise (never gists — enforced by assembly).
        target_tokens: The block's token budget (``settings.core_token_budget``).

    Returns:
        The :class:`CoreBlock`, or ``None`` when ``sources`` is empty.
    """
    if not sources:
        return None
    content = assemble_summarizer_input(list(sources))  # from-originals or it raises
    text = await summarizer.summarize(content, target_tokens=target_tokens)
    return CoreBlock(text=text, source_ids=tuple(c.id for c in sources))


def core_block_to_chunk(block: CoreBlock, persona_id: str, *, now: datetime) -> PersonaChunk:
    """Project the block into a ``core_memory`` chunk for the append-only write (K9-D-10).

    The stable ``logical_id`` (:func:`core_logical_id`) makes the write supersede the prior
    version; the source ids ride ``metadata`` as the cited provenance (§0). The backend embeds
    the text on write (the column is NOT NULL) — the block is not retrieved by similarity, but
    the embedding is free and keeps the transport uniform.
    """
    return PersonaChunk(
        id=f"{persona_id}::{CORE_MEMORY_KIND}::{uuid.uuid4().hex}",
        text=block.text,
        metadata={CORE_SOURCE_IDS_KEY: ",".join(block.source_ids)},
        created_at=now,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id=core_logical_id(persona_id),
            written_at=now,
        ),
    )


def core_block_from_chunk(chunk: PersonaChunk) -> CoreBlock:
    """Reconstruct the block from its stored ``core_memory`` chunk (the read side)."""
    raw = chunk.metadata.get(CORE_SOURCE_IDS_KEY, "")
    source_ids = tuple(s for s in raw.split(",") if s)
    return CoreBlock(text=chunk.text, source_ids=source_ids)


def read_core_block(store: CoreMemoryWriter, persona_id: str) -> CoreBlock | None:
    """Read the persona's current core block for injection, or ``None`` (byte-identical)."""
    chunk = store.current(persona_id)
    return None if chunk is None else core_block_from_chunk(chunk)


async def refresh_core_block(
    persona_id: str,
    *,
    summarizer: Summarizer,
    sources: Sequence[PersonaChunk],
    store: CoreMemoryWriter,
    target_tokens: int,
    now: datetime,
    written_by: str = "core.refresh",
) -> CoreBlock | None:
    """Rebuild + persist the core block — the BACKGROUND refresh (K9-D-10; never the turn path).

    Rides K8's engine cadence: build the block from the selected raw sources and, if non-empty,
    append a new version to the store (superseding the prior head). Returns the new block, or
    ``None`` when there was nothing to summarise (the prior block stays). This coroutine is only
    ever awaited off the turn path (acceptance-7) — the read path calls :func:`read_core_block`.
    """
    block = await build_core_block(
        summarizer=summarizer, sources=sources, target_tokens=target_tokens
    )
    if block is None:
        return None
    chunk = core_block_to_chunk(block, persona_id, now=now)
    store.write(persona_id, [chunk], source=WriteSource.SYSTEM, written_by=written_by)
    return block
