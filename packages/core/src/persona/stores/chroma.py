"""ChromaDB-backed transport for the four typed stores.

Carries forward the Persona-RAG patterns documented in ``research.md`` §2.1:
- Single ``PersistentClient`` per persistence path.
- One Chroma collection per ``(persona_id, store_kind)`` pair.
- ``embedding_function=None`` — we compute embeddings ourselves and pass
  them in (Persona-RAG decision; avoids loading Chroma's default ONNX
  embedder).
- Cosine distance with L2-normalised vectors → similarity = 1 - distance.
- Query batch cap = 64 to dodge SQLite's host-parameter limit.

This module is the transport layer only. Policy, versioning, and audit
emission live in :class:`persona.stores.base.TypedStore`. The transport
exposes a narrow ``ChromaBackend`` that the typed stores compose with.
"""

from __future__ import annotations

import json
from datetime import datetime  # noqa: TC003 — reinforce() takes a runtime datetime
from pathlib import Path  # noqa: TC003 — used at runtime by ChromaBackend.__init__
from typing import TYPE_CHECKING, Any

from persona.logging import get_logger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource

if TYPE_CHECKING:
    from persona.stores.embedder import Embedder

_log = get_logger("stores.chroma")

__all__ = ["CHROMA_QUERY_BATCH_CAP", "ChromaBackend", "collection_name_for"]

# Persona-RAG hedge against SQLite "too many SQL variables" on large query
# batches (~3,452 BEIR/NQ queries broke at scale). Not a problem at persona
# scale, but the constant survives so the constraint is in one place.
CHROMA_QUERY_BATCH_CAP: int = 64


def collection_name_for(persona_id: str, store_kind: str) -> str:
    """Deterministic collection name per (persona, store_kind).

    Sanitises ``persona_id`` to Chroma's accepted character set (alphanumeric,
    ``_``, ``-``) by replacing other characters with ``_``. Spec 07's
    Postgres backend uses a different naming scheme; the convention here
    is local to Chroma.
    """
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in persona_id)
    return f"persona__{safe}__{store_kind}"


class ChromaBackend:
    """Thin wrapper over a Chroma ``PersistentClient`` and its collections.

    One backend instance owns one persist path; collections inside are
    keyed by ``(persona_id, store_kind)``.
    """

    def __init__(self, *, persist_path: Path, embedder: Embedder) -> None:
        import chromadb

        self.persist_path = persist_path
        self.embedder = embedder
        self.persist_path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.persist_path))
        _log.info("ChromaBackend ready path={path}", path=str(self.persist_path))

    def _collection(self, persona_id: str, store_kind: str) -> Any:  # noqa: ANN401 — Chroma's API
        return self._client.get_or_create_collection(
            name=collection_name_for(persona_id, store_kind),
            configuration={"hnsw": {"space": "cosine"}},
            embedding_function=None,
        )

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
        collection = self._collection(persona_id, store_kind)
        embeddings = self.embedder.encode([c.text for c in chunks])
        collection.upsert(
            ids=[c.id for c in chunks],
            documents=[c.text for c in chunks],
            embeddings=embeddings,
            metadatas=[_chunk_to_metadata(c) for c in chunks],
        )

    def reinforce(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],
        recalled_at: datetime,
    ) -> None:
        """Metadata-only lifecycle bump (K8-D-5) — never a re-embed.

        Reads the current ``k8_*`` metadata for the listed ids and writes the
        bumped values via ``collection.update`` WITHOUT documents/embeddings,
        so Chroma keeps the stored vector untouched. Unknown ids are skipped.
        """
        if not ids:
            return
        collection = self._collection(persona_id, store_kind)
        raw = collection.get(ids=ids, include=["metadatas"])
        got_ids = raw.get("ids") or []
        metas = raw.get("metadatas") or []
        if not got_ids:
            return
        bumped = []
        for meta in metas:
            md = dict(meta or {})
            md["k8_strength"] = int(md.get("k8_strength", 1)) + 1
            md["k8_last_recalled_at"] = recalled_at.isoformat()
            bumped.append(md)
        collection.update(ids=list(got_ids), metadatas=bumped)

    def set_bands(
        self,
        *,
        persona_id: str,
        store_kind: str,
        bands: dict[str, int],
    ) -> None:
        """Batched band materialization (K8 T7) — metadata-only, never a re-embed."""
        if not bands:
            return
        collection = self._collection(persona_id, store_kind)
        ids = list(bands.keys())
        raw = collection.get(ids=ids, include=["metadatas"])
        got_ids = raw.get("ids") or []
        metas = raw.get("metadatas") or []
        if not got_ids:
            return
        updated = []
        for chunk_id, meta in zip(got_ids, metas, strict=False):
            md = dict(meta or {})
            md["k8_band"] = int(bands[str(chunk_id)])
            updated.append(md)
        collection.update(ids=list(got_ids), metadatas=updated)

    def band_histogram(
        self,
        *,
        persona_id: str,
        store_kind: str,
    ) -> dict[int, int]:
        """Band counts over current heads — in-process (documented Liskov)."""
        histogram: dict[int, int] = {}
        for chunk in self.get_all(persona_id=persona_id, store_kind=store_kind):
            if chunk.provenance is not None and chunk.provenance.superseded_by is not None:
                continue
            histogram[chunk.band] = histogram.get(chunk.band, 0) + 1
        return histogram

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        try:
            self._client.delete_collection(collection_name_for(persona_id, store_kind))
        except Exception:  # noqa: BLE001 — Chroma raises a generic NotFoundError-ish thing
            # Treat "collection didn't exist" as success; delete is idempotent.
            return

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        if not ids:
            return
        collection = self._collection(persona_id, store_kind)
        collection.delete(ids=ids)

    # -- read ---------------------------------------------------------------

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        collection = self._collection(persona_id, store_kind)
        raw = collection.get(include=["documents", "metadatas"])
        return _materialise_get(raw)

    def get_by_logical_ids(
        self,
        *,
        persona_id: str,
        store_kind: str,
        logical_ids: list[str],
    ) -> list[PersonaChunk]:
        """All versions of the listed logical chains (K8-D-12).

        Uses Chroma's metadata ``$in`` filter over the serialised
        ``prov_logical_id`` key; chunks without provenance carry no such key
        and are correctly never returned.
        """
        if not logical_ids:
            return []
        collection = self._collection(persona_id, store_kind)
        raw = collection.get(
            where={"prov_logical_id": {"$in": list(logical_ids)}},
            include=["documents", "metadatas"],
        )
        return _materialise_get(raw)

    def count(
        self,
        *,
        persona_id: str,
        store_kind: str,
        include_superseded: bool = False,
    ) -> int:
        """Chunk count. All-versions is Chroma's O(1) ``count()``; heads filter in-process.

        Chroma's ``where`` cannot express "metadata key absent", so the
        current-heads count materialises and filters — acceptable at the
        local/community volumes this transport serves (documented Liskov;
        the Postgres transport pushes both variants down).
        """
        collection = self._collection(persona_id, store_kind)
        if include_superseded:
            return int(collection.count())
        return len(
            [
                c
                for c in self.get_all(persona_id=persona_id, store_kind=store_kind)
                if c.provenance is None or c.provenance.superseded_by is None
            ]
        )

    def recent(
        self,
        *,
        persona_id: str,
        store_kind: str,
        limit: int,
    ) -> list[PersonaChunk]:
        """Current heads, newest first — materialise + sort ``(created_at, id)`` in-process.

        Chroma has no ORDER BY; the in-process sort is the documented Liskov
        trade for the local/community transport. Never sorts by bare id
        (count-era and uuidv7-era formats mix, K8-D-6).
        """
        if limit <= 0:
            return []
        current = [
            c
            for c in self.get_all(persona_id=persona_id, store_kind=store_kind)
            if c.provenance is None or c.provenance.superseded_by is None
        ]
        current.sort(key=lambda c: (c.created_at, c.id), reverse=True)
        return current[:limit]

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[PersonaChunk]:
        collection = self._collection(persona_id, store_kind)
        embeddings = self.embedder.encode([text])
        if not embeddings:
            return []
        # Stay below the SQLite variable cap even when callers ask for one
        # query at a time, because future batched queries (spec 05) will
        # come through the same code path.
        n_results = min(top_k, CHROMA_QUERY_BATCH_CAP)
        # Chroma rejects an empty ``where={}`` ("Expected where to have
        # exactly one operator"); pass ``where`` only when callers gave one.
        kwargs: dict[str, Any] = {
            "query_embeddings": embeddings,
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where
        raw = collection.query(**kwargs)
        return _materialise_query(raw)


# -- materialisation helpers -------------------------------------------------


def _chunk_to_metadata(chunk: PersonaChunk) -> dict[str, str | float | int | bool]:
    """Flatten ``PersonaChunk`` into Chroma's metadata-friendly shape.

    Chroma only accepts JSON-primitive metadata values. We serialise
    provenance into flat string keys (``prov_source``, ``prov_logical_id``,
    ``prov_version``, ...) so we can round-trip cleanly. ``metadata`` itself
    flows through as-is (str values per :class:`PersonaChunk`).
    """
    md: dict[str, str | float | int | bool] = {
        "content_hash": chunk.content_hash,
        "created_at": chunk.created_at.isoformat(),
    }
    md.update(chunk.metadata)
    if chunk.provenance is not None:
        prov = chunk.provenance
        md["prov_source"] = str(prov.source)
        md["prov_logical_id"] = prov.logical_id
        md["prov_version"] = prov.version
        md["prov_written_at"] = prov.written_at.isoformat()
        if prov.superseded_by is not None:
            md["prov_superseded_by"] = prov.superseded_by
        if prov.written_by is not None:
            md["prov_written_by"] = prov.written_by
        if prov.reason is not None:
            md["prov_reason"] = prov.reason
    # Spec K8 lifecycle state (K8-D-2): reserved ``k8_*`` keys, the prov_*
    # serialisation precedent. Hash-excluded model fields — they ride Chroma
    # metadata but are NEVER part of the user metadata or the content hash.
    md["k8_strength"] = chunk.strength
    md["k8_band"] = chunk.band
    md["k8_pinned"] = chunk.pinned
    if chunk.last_recalled_at is not None:
        md["k8_last_recalled_at"] = chunk.last_recalled_at.isoformat()
    if chunk.member_ids:
        md["k8_member_ids"] = json.dumps(list(chunk.member_ids))
    # Capture the user-supplied metadata keys so we can split them out on
    # read without confusing them with our reserved keys.
    md["__user_meta_keys"] = json.dumps(sorted(chunk.metadata.keys()))
    return md


def _materialise_get(raw: dict[str, Any]) -> list[PersonaChunk]:
    ids = raw.get("ids") or []
    docs = raw.get("documents") or []
    metas = raw.get("metadatas") or []
    chunks: list[PersonaChunk] = []
    for chunk_id, text, meta in zip(ids, docs, metas, strict=False):
        chunk = _meta_to_chunk(
            chunk_id=str(chunk_id),
            text=str(text or ""),
            meta=meta or {},
            distance=None,
        )
        chunks.append(chunk)
    return chunks


def _materialise_query(raw: dict[str, Any]) -> list[PersonaChunk]:
    # Chroma's query() returns each field as a list-of-batches; we always
    # send a single query, so batch index 0 is our row.
    ids_batch = raw.get("ids") or []
    docs_batch = raw.get("documents") or []
    meta_batch = raw.get("metadatas") or []
    dist_batch = raw.get("distances") or []
    if not ids_batch:
        return []
    ids = ids_batch[0]
    docs = docs_batch[0] if docs_batch else [None] * len(ids)
    metas = meta_batch[0] if meta_batch else [{}] * len(ids)
    dists = dist_batch[0] if dist_batch else [None] * len(ids)
    chunks: list[PersonaChunk] = []
    for chunk_id, text, meta, dist in zip(ids, docs, metas, dists, strict=False):
        chunks.append(
            _meta_to_chunk(
                chunk_id=str(chunk_id),
                text=str(text or ""),
                meta=meta or {},
                distance=float(dist) if dist is not None else None,
            )
        )
    return chunks


def _meta_to_chunk(
    *,
    chunk_id: str,
    text: str,
    meta: dict[str, Any],
    distance: float | None,
) -> PersonaChunk:
    from datetime import datetime

    user_keys = json.loads(meta.get("__user_meta_keys", "[]"))
    user_metadata = {k: str(meta.get(k, "")) for k in user_keys if k in meta}

    provenance: ChunkProvenance | None = None
    if "prov_source" in meta:
        provenance = ChunkProvenance(
            source=WriteSource(meta["prov_source"]),
            logical_id=str(meta["prov_logical_id"]),
            version=int(meta["prov_version"]),
            superseded_by=(
                str(meta["prov_superseded_by"]) if "prov_superseded_by" in meta else None
            ),
            written_at=datetime.fromisoformat(str(meta["prov_written_at"])),
            written_by=(str(meta["prov_written_by"]) if "prov_written_by" in meta else None),
            reason=str(meta["prov_reason"]) if "prov_reason" in meta else None,
        )
    last_recalled_raw = meta.get("k8_last_recalled_at")
    member_ids_raw = meta.get("k8_member_ids")
    return PersonaChunk(
        id=chunk_id,
        text=text,
        metadata=user_metadata,
        distance=distance,
        content_hash=str(meta.get("content_hash", "")),
        provenance=provenance,
        created_at=datetime.fromisoformat(str(meta["created_at"])),
        strength=int(meta.get("k8_strength", 1)),
        last_recalled_at=(
            datetime.fromisoformat(str(last_recalled_raw)) if last_recalled_raw else None
        ),
        band=int(meta.get("k8_band", 0)),
        pinned=bool(meta.get("k8_pinned", False)),
        member_ids=tuple(json.loads(str(member_ids_raw))) if member_ids_raw else (),
    )
