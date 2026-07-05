"""Chunk primitives shared by every memory store.

A :class:`PersonaChunk` is the atomic unit of typed memory. Every chunk
carries enough metadata to be self-describing across the version chain
(:class:`ChunkProvenance`) and to detect tampering (the SHA-256 ``content_hash``
computed at construction time).

The :class:`WriteSource` enum tags every store mutation with its origin so
the per-store policy table can decide and the audit log can record. See
``docs/specs/spec_01/spec_01_core.md`` §5.4 and D-01-12.
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "CHUNK_ID_INDEX_WIDTH",
    "ChunkProvenance",
    "PersonaChunk",
    "WriteSource",
    "make_chunk_id",
    "mint_chunk_id",
]

# Persona-RAG convention (D-01-2): 4-digit zero-padded index per store.
# Lexicographic sort matches insertion order up to 10,000 chunks per store.
CHUNK_ID_INDEX_WIDTH: int = 4


class WriteSource(StrEnum):
    """The three sources of a store mutation.

    See spec §4.3 (architecture) and §5.2 (spec 01) for the per-store policy
    table that maps each source to allow/reject decisions.

    Values:
        SYSTEM: Runtime/platform automatic writes (episodic write-back,
            consolidation, schema-evolution migrations). The default for the
            store ``write()`` method.
        USER: Explicit owner edits via CLI/API/UI.
        PERSONA_SELF: The persona itself deciding during a conversation/task
            that a value within an existing field should be recorded or
            revised. Never used to add new fields.
    """

    SYSTEM = "system"
    USER = "user"
    PERSONA_SELF = "persona_self"


def make_chunk_id(persona_id: str, store_kind: str, index: int) -> str:
    """Build a deterministic chunk identifier.

    Carries forward the Persona-RAG convention (D-01-2). Format::

        {persona_id}::{store_kind}::{index:04d}

    Args:
        persona_id: The persona's stable identifier.
        store_kind: One of ``identity``, ``self_facts``, ``worldview``,
            ``episodic``.
        index: Zero-based position of the chunk in its store. Must be
            non-negative and fit in 4 decimal digits when padded.

    Returns:
        A string identifier that sorts lexicographically in insertion order.

    Raises:
        ValueError: If ``index`` is negative.
    """
    if index < 0:
        msg = f"chunk index must be non-negative; got {index!r}"
        raise ValueError(msg)
    return f"{persona_id}::{store_kind}::{index:0{CHUNK_ID_INDEX_WIDTH}d}"


def _uuid7() -> uuid.UUID:
    """A UUIDv7 (RFC 9562): 48-bit unix-ms timestamp + 74 random bits.

    Python 3.11 has no ``uuid.uuid7``; this is the standard construction
    (time-ordered among v7 ids, collision-free without coordination). Written
    in-repo per the dependency-minimisation rule (~15 lines beats a package).
    """
    ts_ms = time.time_ns() // 1_000_000
    value = (ts_ms & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76  # version 7
    value |= secrets.randbits(12) << 64  # rand_a
    value |= 0b10 << 62  # RFC 4122 variant
    value |= secrets.randbits(62)  # rand_b
    return uuid.UUID(int=value)


def mint_chunk_id(persona_id: str, store_kind: str) -> str:
    """Mint a race-free, time-ordered chunk identifier (Spec K8, K8-D-6).

    Format ``{persona_id}::{store_kind}::{uuidv7}``. Replaces the count-derived
    :func:`make_chunk_id` on the episodic write path: the count index raced
    under concurrent writers (chat + voice) and its 4-digit padding broke
    lexicographic order past 9999. Old-format ids remain valid forever — no
    reader may assume either format; insertion-order consumers sort by
    ``(created_at, id)``, never by bare id.
    """
    return f"{persona_id}::{store_kind}::{_uuid7()}"


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; pass tz-aware datetimes through.

    Convert non-UTC offsets to UTC so all stored timestamps share a frame.
    Spec §11.4: tz-aware UTC, always.
    """
    if value.tzinfo is None:
        msg = (
            "naive datetime not allowed; use datetime.now(timezone.utc) "
            "or attach a tzinfo (see spec_01_core.md §11.4)"
        )
        raise ValueError(msg)
    return value.astimezone(UTC)


class ChunkProvenance(BaseModel):
    """Audit metadata attached to every mutable-store chunk.

    Identity-store chunks do not carry provenance because identity is
    immutable at runtime (changes require editing the YAML and reloading).
    The other three stores use provenance to walk the version chain and to
    decide per-source policy.

    Attributes:
        source: Which of the three update sources produced this chunk.
        logical_id: Stable identifier grouping all versions of "the same
            fact." On a chunk's first write this equals its ``id``; later
            versions share the same ``logical_id`` but have distinct ``id``
            values (D-01-8).
        version: Monotonic per ``logical_id`` starting at 1.
        superseded_by: The ``id`` of the version that supersedes this one,
            or ``None`` if this is the current version of its logical chain.
        written_at: UTC timestamp of the write. Naive datetimes are rejected.
        written_by: User id, ``"system"``, or a model+tier identifier
            (e.g., ``"frontier:claude-sonnet-4-6"``) for persona_self writes.
        reason: Short free-text rationale. Required for persona_self writes
            (the store enforces this; the model itself does not).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: WriteSource
    logical_id: str
    version: int = Field(default=1, ge=1)
    superseded_by: str | None = None
    written_at: datetime
    written_by: str | None = None
    reason: str | None = None

    @field_validator("written_at", mode="after")
    @classmethod
    def _written_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)


def _sorted_metadata_repr(metadata: dict[str, str]) -> str:
    """Deterministic string repr of a metadata dict.

    Used by ``content_hash`` so the hash is stable across insertion orders.
    Key order in the input does not affect the output.
    """
    items = sorted(metadata.items(), key=lambda kv: kv[0])
    return repr(items)


def _compute_content_hash(text: str, metadata: dict[str, str]) -> str:
    """SHA-256 of ``text`` + sorted-metadata repr.

    Same inputs → same output, regardless of metadata key order.
    """
    payload = text.encode("utf-8") + b"\x00" + _sorted_metadata_repr(metadata).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class PersonaChunk(BaseModel):
    """The atomic unit of typed memory.

    See spec §5.4. Pydantic v2 BaseModel (not a frozen dataclass) so we can
    compute ``content_hash`` via a model validator and enforce tz-aware
    datetimes at the boundary. D-01-12.

    Attributes:
        id: Stable identifier. Conventionally produced by :func:`make_chunk_id`,
            though arbitrary strings are accepted to support migrations.
        text: The chunk's textual content.
        metadata: Arbitrary string-keyed metadata. Chroma's storage layer
            only accepts JSON-primitive values, so we restrict to strings.
        distance: Set by ``MemoryStore.query`` on retrieved chunks; never
            populated by writers.
        content_hash: SHA-256 of ``text`` + sorted-metadata repr. Computed
            at construction if not supplied; if supplied, it must match the
            computed value (tamper check).
        provenance: Audit metadata for mutable-store chunks. ``None`` for
            identity-store chunks.
        created_at: UTC creation timestamp. Naive datetimes are rejected.
        strength: Usage-reinforcement counter (Spec K8, K8-D-3/5): recall
            increments it and resets the decay clock. Hash-EXCLUDED lifecycle
            state — mutating it never changes chunk identity or forces a
            re-embed (the ``distance``/``provenance`` class, NOT the
            ``metadata`` class).
        last_recalled_at: When the chunk last surfaced in recall (the decay
            clock's reset point; ``created_at`` is the fallback). Hash-excluded.
        band: The fidelity band of the chunk's DISPLAY (K8-D-3: 0 = FULL/raw
            text; 1 = gist). Materialized by the background tiering pass; a
            stale value between passes is harmless (display fidelity only,
            never correctness — raw text + embedding are permanent).
            Hash-excluded.
        pinned: The chunk-level constraint class (K8-D-3 as amended at T2):
            pinned chunks never demote out of FULL display. First-class and
            hash-excluded — pin-toggling must never trip the tamper check.
        member_ids: On gist rows (``store_kind='episodic_gist'``) the ordered
            ids of the raw member chunks this gist summarises (the drill-down
            pointers, K8-D-2). Empty on raw chunks. Hash-excluded.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    text: str
    metadata: dict[str, str] = Field(default_factory=dict)
    distance: float | None = None
    content_hash: str = ""
    provenance: ChunkProvenance | None = None
    created_at: datetime
    # --- Spec K8 lifecycle state (hash-excluded by construction: the content
    # hash covers text + metadata only — see _populate_or_verify_content_hash).
    strength: int = Field(default=1, ge=1)
    last_recalled_at: datetime | None = None
    band: int = Field(default=0, ge=0)
    pinned: bool = False
    member_ids: tuple[str, ...] = ()

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)

    @model_validator(mode="after")
    def _populate_or_verify_content_hash(self) -> PersonaChunk:
        expected = _compute_content_hash(self.text, self.metadata)
        if not self.content_hash:
            # Frozen models forbid setattr; rebuild via model_copy to inject
            # the computed hash without going through __setattr__.
            object.__setattr__(self, "content_hash", expected)
            return self
        if self.content_hash != expected:
            msg = f"content_hash mismatch: expected {expected!r}, got {self.content_hash!r}"
            raise ValueError(msg)
        return self
