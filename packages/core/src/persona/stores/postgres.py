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
from sqlalchemy.exc import IntegrityError, OperationalError

from persona.errors import ChainConflictError, ForgottenEvidenceError, MemoryOwnerMissingError
from persona.logging import get_logger
from persona.schema.chunks import (
    ChunkProvenance,
    PersonaChunk,
    WriteSource,
    verify_content_hash,
)
from persona.stores.guarded import check_requires_kinds
from persona.stores.orphans import OrphanSweep, role_sees_every_tenant

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy import Connection, Engine

    from persona.stores.embedder import Embedder

_log = get_logger("stores.postgres")

__all__ = ["CHAIN_HEAD_INDEX", "EMBEDDING_DIM", "PostgresBackend"]

# bge-small-en-v1.5. Must match the api migration's vector(384). A contract
# test (T07) asserts both ends agree.
EMBEDDING_DIM: int = 384

# The partial unique index that allows one current version per chain (R9-205). Named here
# because ``upsert`` has to recognise it; the api owns the DDL, and a contract test asserts
# that this is the name the api declares.
CHAIN_HEAD_INDEX: str = "uq_memory_chain_head"

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


#: How the guard in migration 062 identifies itself.
_FOREIGN_KEY_VIOLATION = "23503"
#: Postgres broke a lock cycle by failing this transaction. Nothing of it landed.
_DEADLOCK_DETECTED = "40P01"
_OWNER_GUARD_PREFIX = "memory_chunks_owner_guard:"


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
        supersedes: Mapping[str, tuple[int, str]] | None = None,
        requires: Sequence[str] = (),
        requires_kinds: Sequence[str] = (),
    ) -> None:
        """Write ``chunks`` in one transaction, after checking what the write read (R9-271).

        Inside that transaction, in this order: the cited evidence is locked ``FOR KEY SHARE``,
        so a forget deleting it waits for this to commit and then finds what it wrote; the
        superseded versions are locked ``FOR UPDATE`` and compared with what the write read;
        their links move, and only their links (the prior head's text is never sent or embedded
        again); then the new rows. Each lock is taken in id order, which every delete of memory
        rows also uses, so two of these cannot wait on each other in a circle. Embedding happens
        before the transaction, so no lock is held while a model runs.
        """
        if not chunks and not supersedes:
            return
        check_requires_kinds(requires, requires_kinds)
        # Before the embedder: a stale chunk must not be paid for, and must not land (R9-210).
        for chunk in chunks:
            verify_content_hash(chunk)
        embeddings = self._embedder.encode([c.text for c in chunks]) if chunks else []
        rows: list[dict[str, Any]] = []
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            if len(embedding) != EMBEDDING_DIM:
                msg = (
                    f"embedding dim {len(embedding)} != expected {EMBEDDING_DIM} "
                    f"for chunk {chunk.id!r}"
                )
                raise ValueError(msg)
            rows.append(self._chunk_to_row(persona_id, store_kind, chunk, embedding))
        # INSERT ... ON CONFLICT (id) DO UPDATE: upsert by chunk id, matching
        # ChromaBackend.upsert's replace-on-same-id semantics.
        stmt = insert(_memory_chunks)
        update_cols = {c.name: stmt.excluded[c.name] for c in _memory_chunks.c if c.name != "id"}
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols)
        try:
            with self._engine.begin() as conn:
                if requires:
                    _lock_evidence(conn, persona_id, store_kind, requires, requires_kinds)
                if supersedes:
                    _move_links(conn, persona_id, store_kind, supersedes)
                if rows:
                    conn.execute(stmt, rows)
        except IntegrityError as exc:
            if _is_owner_guard_refusal(exc):
                raise MemoryOwnerMissingError(
                    "the persona or conversation this memory belongs to no longer exists",
                    context={"store_kind": store_kind},
                ) from None
            if not _is_chain_head_conflict(exc):
                raise
            # The whole statement list rolled back, so nothing landed.
            raise _chain_conflict(persona_id, store_kind) from None
        except OperationalError as exc:
            if not _is_deadlock(exc):
                raise
            # Rolled back whole. The store reads again and retries, as for any conflict.
            raise _chain_conflict(persona_id, store_kind) from None

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

    def relink(
        self,
        *,
        persona_id: str,
        store_kind: str,
        links: dict[str, str | None],
        versions: dict[str, int] | None = None,
    ) -> None:
        """Repair primitive (Spec K13, D-K13-6): rewrite ``superseded_by``, nothing else.

        One ``UPDATE`` per distinct target inside ONE transaction, the ``set_bands`` shape.
        The embedding, the text and the metadata are never touched, so no embedder is needed
        and the stored vector cannot drift; ``content_hash`` excludes provenance, so chunk
        identity is stable too. Unknown ids match zero rows, which is the intended no-op.
        ``versions`` renumbers provenance in the same transaction (R9-205).
        """
        if not links and not versions:
            return
        with self._engine.begin() as conn:
            self._relink_in(conn, persona_id, store_kind, links, versions or {})

    def _relink_in(
        self,
        conn: Connection,
        persona_id: str,
        store_kind: str,
        links: dict[str, str | None],
        versions: dict[str, int],
    ) -> None:
        """Move version pointers inside the caller's transaction."""
        by_target: dict[str | None, list[str]] = {}
        for chunk_id, target in links.items():
            by_target.setdefault(target, []).append(chunk_id)
        for target, ids in by_target.items():
            conn.execute(
                _memory_chunks.update()
                .where(
                    _memory_chunks.c.persona_id == persona_id,
                    _memory_chunks.c.kind == store_kind,
                    _memory_chunks.c.id.in_(ids),
                )
                .values(superseded_by=target)
            )
        for chunk_id, version in versions.items():
            conn.execute(
                _memory_chunks.update()
                .where(
                    _memory_chunks.c.persona_id == persona_id,
                    _memory_chunks.c.kind == store_kind,
                    _memory_chunks.c.id == chunk_id,
                )
                .values(version=version)
            )

    def excise_versions(
        self,
        *,
        persona_id: str,
        store_kind: str,
        logical_id: str,
        expected: dict[str, tuple[int, str | None]],
        doomed_ids: list[str],
        links: dict[str, str | None],
        versions: dict[str, int],
    ) -> None:
        """Delete these versions and close the chain, in one transaction (R9-209).

        The chain is locked and compared with ``expected`` first, so a plan made from a read
        another writer has moved past never lands; the lock holds a writer updating any of
        these rows until this commits. Then the delete, because a doomed head has to lose its
        NULL before a survivor gains one, or the head index would see two for a statement.
        """
        if not doomed_ids and not links and not versions:
            return
        try:
            with self._engine.begin() as conn:
                found = conn.execute(
                    select(
                        _memory_chunks.c.id,
                        _memory_chunks.c.version,
                        _memory_chunks.c.superseded_by,
                    )
                    .where(
                        _memory_chunks.c.persona_id == persona_id,
                        _memory_chunks.c.kind == store_kind,
                        _memory_chunks.c.logical_id == logical_id,
                    )
                    .order_by(_memory_chunks.c.id)
                    .with_for_update()
                ).all()
                if {str(i): (int(v), s) for i, v, s in found} != expected:
                    raise _chain_conflict(persona_id, store_kind)
                conn.execute(
                    delete(_memory_chunks).where(
                        _memory_chunks.c.persona_id == persona_id,
                        _memory_chunks.c.kind == store_kind,
                        _memory_chunks.c.id.in_(doomed_ids),
                    )
                )
                self._relink_in(conn, persona_id, store_kind, links, versions)
        except IntegrityError as exc:
            if not _is_chain_head_conflict(exc):
                raise
            # A plan made from a read another writer has since moved past. Rolled back whole.
            raise _chain_conflict(persona_id, store_kind) from None

    def persona_ids(self, store_kinds: tuple[str, ...]) -> list[str]:
        """Every persona with rows of these kinds, for an operator's sweep (R9-211).

        On this transport and not the protocol: the sweep needs the whole database in one
        place, which is what a Chroma folder per deployment is not.
        """
        stmt = (
            select(_memory_chunks.c.persona_id)
            .where(_memory_chunks.c.kind.in_(store_kinds))
            .distinct()
            .order_by(_memory_chunks.c.persona_id)
        )
        with self._engine.connect() as conn:
            return [str(row) for row in conn.execute(stmt).scalars().all()]

    def orphan_sweep(self) -> OrphanSweep:
        """The sweep for rows whose owner is gone, on this database (R9-284)."""
        return OrphanSweep(self._engine)

    def sees_every_tenant(self) -> bool:
        """Whether this connection's role can read past row level security.

        A sweep as ``persona_app`` reads zero rows and would report a healthy database, which
        is the worst possible answer from a check run during a restore.
        """
        return role_sees_every_tenant(self._engine)

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
        """Delete the listed rows, locking them first in id order (R9-271).

        The lock goes first, and in the order a guarded write locks its evidence, so a forget
        and a write citing several of the same rows queue behind each other instead of each
        holding half and waiting for the other.
        """
        if not ids:
            return
        where = (
            _memory_chunks.c.persona_id == persona_id,
            _memory_chunks.c.kind == store_kind,
            _memory_chunks.c.id.in_(ids),
        )
        with self._engine.begin() as conn:
            conn.execute(
                select(_memory_chunks.c.id)
                .where(*where)
                .order_by(_memory_chunks.c.id)
                .with_for_update()
            )
            conn.execute(delete(_memory_chunks).where(*where))

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

    def get_by_ids(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],
    ) -> list[PersonaChunk]:
        """Rows by primary key, scoped to the persona and kind (Spec K13, T1)."""
        if not ids:
            return []
        stmt = select(_memory_chunks).where(
            _memory_chunks.c.persona_id == persona_id,
            _memory_chunks.c.kind == store_kind,
            _memory_chunks.c.id.in_(ids),
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
                # A similarity query is the CURRENT view: a superseded version must never
                # occupy a slot in it. Filtering here rather than after the limit is what
                # makes ``top_k`` mean top_k current chunks (the store re-checks, but a
                # post-filter alone silently shrank every result set that met an edited fact).
                _memory_chunks.c.superseded_by.is_(None),
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


def _chain_conflict(persona_id: str, store_kind: str) -> ChainConflictError:
    """The domain error for a head index violation, with the driver's error left behind.

    Raised ``from None`` on purpose (round 2 review, LOW-4). The driver's error quotes the
    statement's parameters, which are the rows being written, so chaining it would put a
    person's memory into any log that prints the traceback, and the core refresh does. The
    constraint name is all a reader needs, and it is in the context.
    """
    msg = "another writer landed on this version chain first"
    return ChainConflictError(
        msg,
        context={"persona_id": persona_id, "store": store_kind, "constraint": CHAIN_HEAD_INDEX},
    )


def _lock_evidence(
    conn: Connection,
    persona_id: str,
    store_kind: str,
    requires: Sequence[str],
    requires_kinds: Sequence[str],
) -> None:
    """Lock the cited rows ``FOR KEY SHARE`` in id order, and refuse if any is gone (R9-271).

    ``FOR KEY SHARE`` is what a foreign key check takes: it holds off a DELETE of the row until
    this transaction ends, and lets ``reinforce`` and ``set_bands`` through, since they only
    update columns that are not the key.
    """
    wanted = sorted(set(requires))
    found = set(
        conn.execute(
            select(_memory_chunks.c.id)
            .where(
                _memory_chunks.c.persona_id == persona_id,
                _memory_chunks.c.kind.in_(list(requires_kinds)),
                _memory_chunks.c.id.in_(wanted),
            )
            .order_by(_memory_chunks.c.id)
            .with_for_update(read=True, key_share=True)
        )
        .scalars()
        .all()
    )
    missing = len(set(wanted) - found)
    if missing:
        raise ForgottenEvidenceError(
            "the evidence this memory was built from has been forgotten",
            context={"persona_id": persona_id, "store": store_kind, "missing": str(missing)},
        )


def _move_links(
    conn: Connection,
    persona_id: str,
    store_kind: str,
    supersedes: Mapping[str, tuple[int, str]],
) -> None:
    """Compare-and-set each superseded version's link, under its row lock (R9-271).

    Each version must still be there, current, and at the number the writer read. A forget that
    deleted it, or renumbered it while closing up a chain, makes the write stale, and a stale
    write raises rather than landing a copy of a row the person asked to be rid of.
    """
    priors = sorted(supersedes)
    found = {
        str(chunk_id): (version, superseded_by)
        for chunk_id, version, superseded_by in conn.execute(
            select(
                _memory_chunks.c.id,
                _memory_chunks.c.version,
                _memory_chunks.c.superseded_by,
            )
            .where(
                _memory_chunks.c.persona_id == persona_id,
                _memory_chunks.c.kind == store_kind,
                _memory_chunks.c.id.in_(priors),
            )
            .order_by(_memory_chunks.c.id)
            .with_for_update()
        ).all()
    }
    if found != {prior: (supersedes[prior][0], None) for prior in priors}:
        raise _chain_conflict(persona_id, store_kind)
    # The lock above is what makes the comparison exact. The UPDATE says the same thing again and
    # counts what it moved, so the move stays right if that lock were ever lost (PR #39 review).
    for prior in priors:
        moved = conn.execute(
            _memory_chunks.update()
            .where(
                _memory_chunks.c.persona_id == persona_id,
                _memory_chunks.c.kind == store_kind,
                _memory_chunks.c.id == prior,
                _memory_chunks.c.version == supersedes[prior][0],
                _memory_chunks.c.superseded_by.is_(None),
            )
            .values(superseded_by=supersedes[prior][1])
        )
        if moved.rowcount != 1:
            raise _chain_conflict(persona_id, store_kind)


def _is_deadlock(exc: OperationalError) -> bool:
    """Whether Postgres failed this transaction to break a lock cycle."""
    return getattr(exc.orig, "sqlstate", None) == _DEADLOCK_DETECTED


def _is_owner_guard_refusal(exc: IntegrityError) -> bool:
    """Whether migration 062's trigger refused this write: its error code and its own words."""
    orig = exc.orig
    if getattr(orig, "sqlstate", None) != _FOREIGN_KEY_VIOLATION:
        return False
    message = getattr(getattr(orig, "diag", None), "message_primary", None)
    return isinstance(message, str) and message.startswith(_OWNER_GUARD_PREFIX)


def _is_chain_head_conflict(exc: IntegrityError) -> bool:
    """Whether this violation is the head index and not some other constraint on the table."""
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == CHAIN_HEAD_INDEX


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
