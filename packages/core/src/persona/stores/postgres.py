"""Postgres + pgvector transport for the four typed stores (spec 07).

The production sibling of :class:`persona.stores.chroma.ChromaBackend`. It
satisfies the same :class:`persona.stores.backend.Backend` protocol, so the four
typed stores compose it unchanged — policy, versioning, audit, and episodic
decay all live above it in :mod:`persona.stores.base` / ``episodic`` and are
*not* re-implemented here. This module is the transport only.

Decisions in force (see ``docs/specs/spec_07/decisions.md``):

- **D-07-1:** synchronous psycopg3 (``postgresql+psycopg://``). No event loop,
  no bridge — the ``MemoryStore`` protocol the runtime consumes is sync.
- **D-07-2:** SQLAlchemy Core. ``persona-core`` cannot import the api package,
  so this module defines its *own* minimal ``memory_chunks`` table view; a
  contract test (T07) asserts it matches the api-owned migrated schema.
- **D-07-4:** provenance/versioning round-trips through promoted columns
  (``logical_id``/``version``/``superseded_by`` + the ``ChunkProvenance``
  fields); user metadata lives in the ``metadata`` JSONB column.
- **decay:** ``query`` returns raw cosine-distance ranking with ``distance``
  populated; ``EpisodicStore`` re-ranks by ``exp(-elapsed/tau)`` in Python
  (D-01-4). There is deliberately NO decay SQL (the spec's §4.3 is superseded).
- **embedding dim:** vectors must be exactly :data:`EMBEDDING_DIM` long; a
  mismatch fails fast at write (no truncation/padding).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, insert

from persona.logging import get_logger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from persona.stores.embedder import Embedder

_log = get_logger("stores.postgres")

__all__ = ["EMBEDDING_DIM", "PostgresBackend"]

# bge-small-en-v1.5. Must match the api migration's vector(384). A contract
# test (T07) asserts both ends agree.
EMBEDDING_DIM: int = 384

# This module's OWN minimal view of memory_chunks (core cannot import the api
# schema). Column names/types mirror persona_api.db.models.memory_chunks; the
# contract test guards drift.
_md = MetaData()
_memory_chunks = Table(
    "memory_chunks",
    _md,
    Column("id", Text, primary_key=True),
    Column("persona_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("metadata", JSONB, nullable=False),
    Column("logical_id", Text),
    Column("version", Integer),
    Column("superseded_by", Text),
    Column("prov_source", Text),
    Column("written_at", DateTime(timezone=True)),
    Column("written_by", Text),
    Column("reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # --- Spec K8 lifecycle columns (K8-D-2; hash-excluded PersonaChunk state).
    Column("strength", Integer, nullable=False, server_default=text("1")),
    Column("last_recalled_at", DateTime(timezone=True)),
    Column("fidelity_band", SmallInteger, nullable=False, server_default=text("0")),
    Column("pinned", Boolean, nullable=False, server_default=text("false")),
    Column("member_ids", ARRAY(Text)),
)


class PostgresBackend:
    """Transport over a Postgres + pgvector engine.

    One backend instance owns one engine/pool; rows are partitioned by
    ``(persona_id, kind)``. The embedder is injected (DI), never imported.

    Args:
        engine: A *synchronous* SQLAlchemy engine (``postgresql+psycopg://``).
            The caller (the API composition root, spec 08; or a test) owns its
            lifecycle and any RLS ``set_config`` plumbing — the transport just
            issues parameterised statements on connections from the engine.
        embedder: Computes L2-normalised embeddings. Must report
            ``dimension == EMBEDDING_DIM``.
        embedding_model: Recorded per row for re-index bookkeeping.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        embedder: Embedder,
        embedding_model: str = "bge-small-en-v1.5",
    ) -> None:
        self._engine = engine
        self._embedder = embedder
        self._embedding_model = embedding_model
        _log.info("PostgresBackend ready model={model}", model=embedding_model)

    # -- mutate -------------------------------------------------------------

    def upsert(
        self,
        *,
        persona_id: str,
        store_kind: str,
        chunks: list[PersonaChunk],
    ) -> None:
        if not chunks:
            return
        embeddings = self._embedder.encode([c.text for c in chunks])
        rows: list[dict[str, Any]] = []
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            if len(embedding) != EMBEDDING_DIM:
                msg = (
                    f"embedding dim {len(embedding)} != expected {EMBEDDING_DIM} "
                    f"for chunk {chunk.id!r}"
                )
                raise ValueError(msg)
            rows.append(self._chunk_to_row(persona_id, store_kind, chunk, embedding))
        # INSERT ... ON CONFLICT (id) DO UPDATE — upsert by chunk id, matching
        # ChromaBackend.upsert's replace-on-same-id semantics.
        stmt = insert(_memory_chunks)
        update_cols = {c.name: stmt.excluded[c.name] for c in _memory_chunks.c if c.name != "id"}
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols)
        with self._engine.begin() as conn:
            conn.execute(stmt, rows)

    def reinforce(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],
        recalled_at: datetime,
    ) -> None:
        """One batched ``UPDATE`` for the turn's recalled ids (K8-D-5).

        Touches only the hash-excluded lifecycle columns — text/embedding/
        metadata are untouched, so identity is stable by construction.
        """
        if not ids:
            return
        stmt = (
            _memory_chunks.update()
            .where(
                _memory_chunks.c.persona_id == persona_id,
                _memory_chunks.c.kind == store_kind,
                _memory_chunks.c.id.in_(ids),
            )
            .values(
                strength=_memory_chunks.c.strength + 1,
                last_recalled_at=recalled_at,
            )
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def set_bands(
        self,
        *,
        persona_id: str,
        store_kind: str,
        bands: dict[str, int],
    ) -> None:
        """Batched band materialization (K8 T7) — one UPDATE per distinct band value."""
        if not bands:
            return
        by_band: dict[int, list[str]] = {}
        for chunk_id, band in bands.items():
            by_band.setdefault(band, []).append(chunk_id)
        with self._engine.begin() as conn:
            for band, ids in by_band.items():
                conn.execute(
                    _memory_chunks.update()
                    .where(
                        _memory_chunks.c.persona_id == persona_id,
                        _memory_chunks.c.kind == store_kind,
                        _memory_chunks.c.id.in_(ids),
                    )
                    .values(fidelity_band=band)
                )

    def band_histogram(
        self,
        *,
        persona_id: str,
        store_kind: str,
    ) -> dict[int, int]:
        """``GROUP BY fidelity_band`` over current heads (the P8 counter surface)."""
        stmt = (
            select(_memory_chunks.c.fidelity_band, func.count())
            .where(
                _memory_chunks.c.persona_id == persona_id,
                _memory_chunks.c.kind == store_kind,
                _memory_chunks.c.superseded_by.is_(None),
            )
            .group_by(_memory_chunks.c.fidelity_band)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return {int(band): int(n) for band, n in rows}

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        stmt = delete(_memory_chunks).where(
            _memory_chunks.c.persona_id == persona_id,
            _memory_chunks.c.kind == store_kind,
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def delete_documents(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],
    ) -> None:
        if not ids:
            return
        stmt = delete(_memory_chunks).where(
            _memory_chunks.c.persona_id == persona_id,
            _memory_chunks.c.kind == store_kind,
            _memory_chunks.c.id.in_(ids),
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)

    # -- read ---------------------------------------------------------------

    def get_all(
        self,
        *,
        persona_id: str,
        store_kind: str,
    ) -> list[PersonaChunk]:
        stmt = select(_memory_chunks).where(
            _memory_chunks.c.persona_id == persona_id,
            _memory_chunks.c.kind == store_kind,
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_chunk(dict(r), distance=None) for r in rows]

    def get_by_logical_ids(
        self,
        *,
        persona_id: str,
        store_kind: str,
        logical_ids: list[str],
    ) -> list[PersonaChunk]:
        """All versions of the listed logical chains (indexed; K8-D-12).

        Served by ``idx_memory_persona_kind_logical`` — the scoped write-path
        read that replaced the full-store scan.
        """
        if not logical_ids:
            return []
        stmt = select(_memory_chunks).where(
            _memory_chunks.c.persona_id == persona_id,
            _memory_chunks.c.kind == store_kind,
            _memory_chunks.c.logical_id.in_(logical_ids),
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_chunk(dict(r), distance=None) for r in rows]

    def count(
        self,
        *,
        persona_id: str,
        store_kind: str,
        include_superseded: bool = False,
    ) -> int:
        """``SELECT count(*)`` — the K8 write-path/counter primitive (never ``len(get_all)``)."""
        stmt = (
            select(func.count())
            .select_from(_memory_chunks)
            .where(
                _memory_chunks.c.persona_id == persona_id,
                _memory_chunks.c.kind == store_kind,
            )
        )
        if not include_superseded:
            stmt = stmt.where(_memory_chunks.c.superseded_by.is_(None))
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def recent(
        self,
        *,
        persona_id: str,
        store_kind: str,
        limit: int,
    ) -> list[PersonaChunk]:
        """Current heads, newest first — pushed down to ``ORDER BY … LIMIT`` (K8, acceptance 1).

        Ordering is ``(created_at, id) DESC`` per K8-D-6: ids mix count-era and
        uuidv7-era formats, so ``created_at`` is the insertion-order key and
        ``id`` only a deterministic tiebreak.
        """
        if limit <= 0:
            return []
        stmt = (
            select(_memory_chunks)
            .where(
                _memory_chunks.c.persona_id == persona_id,
                _memory_chunks.c.kind == store_kind,
                _memory_chunks.c.superseded_by.is_(None),
            )
            .order_by(_memory_chunks.c.created_at.desc(), _memory_chunks.c.id.desc())
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_chunk(dict(r), distance=None) for r in rows]

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,
        top_k: int,
        where: dict[str, Any] | None = None,  # noqa: ANN401 — backend-specific filter shape
    ) -> list[PersonaChunk]:
        embeddings = self._embedder.encode([text])
        if not embeddings:
            return []
        q_vec = embeddings[0]
        distance = _memory_chunks.c.embedding.cosine_distance(q_vec).label("distance")
        stmt = (
            select(_memory_chunks, distance)
            .where(
                _memory_chunks.c.persona_id == persona_id,
                _memory_chunks.c.kind == store_kind,
            )
            .order_by(distance)
            .limit(top_k)
        )
        # Optional exact-match metadata filter (the only `where` shape the
        # typed stores currently pass). Applied against the JSONB column.
        for key, value in (where or {}).items():
            stmt = stmt.where(_memory_chunks.c.metadata[key].astext == str(value))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [self._row_to_chunk(dict(r), distance=float(r["distance"])) for r in rows]

    # -- row <-> chunk ------------------------------------------------------

    def _chunk_to_row(
        self,
        persona_id: str,
        store_kind: str,
        chunk: PersonaChunk,
        embedding: list[float],
    ) -> dict[str, Any]:
        prov = chunk.provenance
        return {
            "id": chunk.id,
            "persona_id": persona_id,
            "kind": store_kind,
            "text": chunk.text,
            "embedding": embedding,
            "embedding_model": self._embedding_model,
            "content_hash": chunk.content_hash,
            "metadata": chunk.metadata,
            "logical_id": prov.logical_id if prov is not None else None,
            "version": prov.version if prov is not None else None,
            "superseded_by": prov.superseded_by if prov is not None else None,
            "prov_source": str(prov.source) if prov is not None else None,
            "written_at": prov.written_at if prov is not None else None,
            "written_by": prov.written_by if prov is not None else None,
            "reason": prov.reason if prov is not None else None,
            "created_at": chunk.created_at,
            # Spec K8 lifecycle state (hash-excluded model fields).
            "strength": chunk.strength,
            "last_recalled_at": chunk.last_recalled_at,
            "fidelity_band": chunk.band,
            "pinned": chunk.pinned,
            "member_ids": list(chunk.member_ids) if chunk.member_ids else None,
        }

    def _row_to_chunk(self, row: dict[str, Any], *, distance: float | None) -> PersonaChunk:
        provenance: ChunkProvenance | None = None
        if row.get("prov_source") is not None:
            provenance = ChunkProvenance(
                source=WriteSource(row["prov_source"]),
                logical_id=str(row["logical_id"]),
                version=int(row["version"]),
                superseded_by=row.get("superseded_by"),
                written_at=_as_utc(row["written_at"]),
                written_by=row.get("written_by"),
                reason=row.get("reason"),
            )
        member_ids = row.get("member_ids")
        last_recalled = row.get("last_recalled_at")
        return PersonaChunk(
            id=str(row["id"]),
            text=str(row["text"]),
            metadata=_as_str_dict(row.get("metadata")),
            distance=distance,
            content_hash=str(row["content_hash"]),
            provenance=provenance,
            created_at=_as_utc(row["created_at"]),
            strength=int(row.get("strength") or 1),
            last_recalled_at=_as_utc(last_recalled) if last_recalled is not None else None,
            band=int(row.get("fidelity_band") or 0),
            pinned=bool(row.get("pinned") or False),
            member_ids=tuple(member_ids) if member_ids else (),
        )


def _as_utc(value: datetime) -> datetime:
    """Ensure a tz-aware UTC datetime (Postgres TIMESTAMPTZ → aware)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_str_dict(value: Any) -> dict[str, str]:  # noqa: ANN401 — JSONB comes back as Any
    """Coerce a JSONB column value into the ``dict[str, str]`` PersonaChunk shape."""
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    return {str(k): str(v) for k, v in dict(value).items()}
