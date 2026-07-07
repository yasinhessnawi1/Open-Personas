"""Shared builders for the K9 recall tests — real chunks + nodes, no store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import PersonaChunk, WriteSource

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=UTC)


def chunk(
    chunk_id: str,
    *,
    distance: float | None = None,
    text: str = "c",
    importance: float | None = None,
    age_hours: float = 0.0,
    strength: int = 1,
) -> PersonaChunk:
    """A raw episodic chunk with optional distance, importance, age, and strength."""
    metadata = {} if importance is None else {"importance": str(importance)}
    created = NOW - timedelta(hours=age_hours)
    return PersonaChunk(
        id=chunk_id,
        text=text,
        distance=distance,
        created_at=created,
        metadata=metadata,
        strength=strength,
    )


def gist(gist_id: str, *, members: tuple[str, ...], distance: float | None = None) -> PersonaChunk:
    """A gist row (band 1, member_ids = the drill pointers)."""
    return PersonaChunk(
        id=gist_id,
        text="gist",
        distance=distance,
        created_at=NOW,
        band=1,
        member_ids=members,
    )


def node(
    node_id: str,
    *,
    distance: float | None = None,
    name: str = "n",
    content: str = "x",
    age_hours: float = 0.0,
) -> ConceptNode:
    """A graph concept node with an optional query-time distance and provenance age."""
    written = NOW - timedelta(hours=age_hours)
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=name,
        content=content,
        distance=distance,
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=written),),
        created_at=written,
    )
