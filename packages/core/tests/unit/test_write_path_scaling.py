"""Spec K8 T1a/T1b — the O(N)-write fix + minted ids (K8-D-6/12, acceptance 1).

Pins the three structural properties of the fast-track fix:

1. **No full-store read on any write path.** ``TypedStore.write`` fetches only
   the batch's logical chains (``get_by_logical_ids``); non-versioning stores
   fetch nothing; ``TypedStore.recent`` delegates to the backend push-down.
   ``get_all`` never appears on these paths.
2. **Versioning SEMANTICS survive the scoped fetch** (K8-D-12 bar): a
   genuinely-versioned kind still finds its true prior head (version N+1 +
   supersede link); an episodic fresh chain stays version 1.
3. **Minted ids** (K8-D-6): uuidv7 format, race-free uniqueness, and the
   ordering sweep — insertion-order consumers sort by ``(created_at, id)`` and
   old count-era ids remain valid alongside minted ones (no format assumption).
"""

# ruff: noqa: ARG002 — the spy backend ignores protocol args by design.
from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from persona.audit import MemoryAuditLogger
from persona.schema.chunks import (
    ChunkProvenance,
    PersonaChunk,
    WriteSource,
    make_chunk_id,
    mint_chunk_id,
)
from persona.stores.episodic import EpisodicStore
from persona.stores.identity import IdentityStore
from persona.stores.self_facts import SelfFactsStore

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
_UUID7_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


class _SpyBackend:
    """In-memory Backend that records which read methods each write path uses."""

    def __init__(self) -> None:
        self.chunks: dict[tuple[str, str], dict[str, PersonaChunk]] = {}
        self.calls: list[str] = []

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        self.calls.append("upsert")
        bucket = self.chunks.setdefault((persona_id, store_kind), {})
        for chunk in chunks:
            bucket[chunk.id] = chunk

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,
        top_k: int,
        where: dict[str, Any] | None = None,  # noqa: ANN401
    ) -> list[PersonaChunk]:
        self.calls.append("query")
        return []

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        self.calls.append("get_all")
        return list(self.chunks.get((persona_id, store_kind), {}).values())

    def count(self, *, persona_id: str, store_kind: str, include_superseded: bool = False) -> int:
        self.calls.append("count")
        chunks = self.chunks.get((persona_id, store_kind), {}).values()
        if include_superseded:
            return len(list(chunks))
        return len(
            [c for c in chunks if c.provenance is None or c.provenance.superseded_by is None]
        )

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        self.calls.append("recent")
        current = [
            c
            for c in self.chunks.get((persona_id, store_kind), {}).values()
            if c.provenance is None or c.provenance.superseded_by is None
        ]
        current.sort(key=lambda c: (c.created_at, c.id), reverse=True)
        return current[:limit]

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        self.calls.append("get_by_logical_ids")
        wanted = set(logical_ids)
        return [
            c
            for c in self.chunks.get((persona_id, store_kind), {}).values()
            if c.provenance is not None and c.provenance.logical_id in wanted
        ]

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.calls.append("delete_persona")
        self.chunks.pop((persona_id, store_kind), None)

    def reinforce(
        self, *, persona_id: str, store_kind: str, ids: list[str], recalled_at: object
    ) -> None:
        from datetime import datetime as _dt

        assert isinstance(recalled_at, _dt)
        self.calls.append("reinforce")
        bucket = self.chunks.get((persona_id, store_kind), {})
        for cid in ids:
            if cid in bucket:
                c = bucket[cid]
                bucket[cid] = c.model_copy(
                    update={"strength": c.strength + 1, "last_recalled_at": recalled_at}
                )

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        self.calls.append("delete_documents")
        bucket = self.chunks.get((persona_id, store_kind), {})
        for chunk_id in ids:
            bucket.pop(chunk_id, None)

    def set_bands(self, *, persona_id: str, store_kind: str, bands: dict[str, int]) -> None:
        self.calls.append("set_bands")
        bucket = self.chunks.get((persona_id, store_kind), {})
        for cid, band in bands.items():
            if cid in bucket:
                bucket[cid] = bucket[cid].model_copy(update={"band": band})

    def band_histogram(self, *, persona_id: str, store_kind: str) -> dict[int, int]:
        histogram: dict[int, int] = {}
        for c in self.chunks.get((persona_id, store_kind), {}).values():
            if c.provenance is None or c.provenance.superseded_by is None:
                histogram[c.band] = histogram.get(c.band, 0) + 1
        return histogram


def _chunk(
    chunk_id: str, *, created_at: datetime = _NOW, logical_id: str | None = None
) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=f"text for {chunk_id}",
        created_at=created_at,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id=logical_id or chunk_id,
            version=1,
            written_at=created_at,
            written_by="test",
        ),
    )


# --- 1. no full-store read on the write/recent paths -------------------------


def test_versioned_write_fetches_only_the_batch_chains_never_get_all() -> None:
    backend = _SpyBackend()
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write("p1", [_chunk("p1::self_facts::0001")], source=WriteSource.USER)
    assert "get_by_logical_ids" in backend.calls
    assert "get_all" not in backend.calls


def test_non_versioning_store_write_fetches_nothing_at_all() -> None:
    # IdentityStore is the shipped SUPPORTS_VERSIONING=False store, but its
    # policy rejects every runtime write (immutable at runtime), so the guard
    # is pinned through a minimal accepting subclass: no versioning ⇒ the
    # write performs ZERO read calls.
    from persona.stores.base import TypedStore
    from persona.stores.policy import PolicyDecision, PolicyRule

    class _NoVersioningStore(TypedStore):
        STORE_KIND = "identity"
        SUPPORTS_VERSIONING = False
        _POLICY = {WriteSource.SYSTEM: PolicyRule(decision=PolicyDecision.ACCEPT)}  # noqa: RUF012

    backend = _SpyBackend()
    store = _NoVersioningStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write(
        "p1",
        [PersonaChunk(id="p1::identity::0000", text="core identity", created_at=_NOW)],
        source=WriteSource.SYSTEM,
    )
    assert "get_by_logical_ids" not in backend.calls
    assert "get_all" not in backend.calls
    assert "upsert" in backend.calls


def test_shipped_identity_store_still_rejects_runtime_writes() -> None:
    # The K8 guard move must not have loosened identity immutability.
    import pytest
    from persona.errors import RuntimeWriteForbiddenError

    backend = _SpyBackend()
    store = IdentityStore(backend=backend, audit_logger=MemoryAuditLogger())
    with pytest.raises(RuntimeWriteForbiddenError):
        store.write(
            "p1",
            [PersonaChunk(id="p1::identity::0000", text="x", created_at=_NOW)],
            source=WriteSource.SYSTEM,
            force=True,
        )
    assert backend.calls == []  # rejected at the boundary: no reads, no writes


def test_typed_store_recent_delegates_to_the_backend_pushdown() -> None:
    backend = _SpyBackend()
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write("p1", [_chunk(mint_chunk_id("p1", "episodic"))])
    backend.calls.clear()
    store.recent("p1", limit=5)
    assert backend.calls == ["recent"]  # never get_all + sort-in-Python


def test_recent_with_non_positive_limit_short_circuits() -> None:
    backend = _SpyBackend()
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    assert store.recent("p1", limit=0) == []
    assert backend.calls == []


# --- 2. versioning semantics preserved under the scoped fetch ----------------


def test_versioned_kind_still_finds_its_true_prior_head() -> None:
    backend = _SpyBackend()
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    first = _chunk("p1::self_facts::0001")
    store.write("p1", [first], source=WriteSource.USER)

    # Second write to the SAME logical chain → version 2 + supersede link.
    second = PersonaChunk(
        id="p1::self_facts::0002",
        text="updated fact",
        created_at=_NOW + timedelta(minutes=1),
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=first.provenance.logical_id,  # type: ignore[union-attr]
            version=1,  # store recomputes
            written_at=_NOW + timedelta(minutes=1),
        ),
    )
    store.write("p1", [second], source=WriteSource.USER)

    stored = backend.chunks[("p1", "self_facts")]
    heads = [c for c in stored.values() if c.provenance and c.provenance.superseded_by is None]
    olds = [c for c in stored.values() if c.provenance and c.provenance.superseded_by is not None]
    assert len(heads) == 1
    assert heads[0].provenance is not None
    assert heads[0].provenance.version == 2  # noqa: PLR2004
    assert len(olds) == 1
    assert olds[0].provenance is not None
    assert olds[0].provenance.superseded_by == heads[0].id


def test_episodic_fresh_chains_stay_version_one() -> None:
    backend = _SpyBackend()
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    for _ in range(3):
        store.write("p1", [_chunk(mint_chunk_id("p1", "episodic"))])
    stored = backend.chunks[("p1", "episodic")]
    assert len(stored) == 3  # noqa: PLR2004
    assert all(c.provenance is not None and c.provenance.version == 1 for c in stored.values())


# --- 3. minted ids (K8-D-6) ---------------------------------------------------


def test_mint_chunk_id_is_a_uuidv7_with_the_store_prefix() -> None:
    minted = mint_chunk_id("p1", "episodic")
    prefix, kind, suffix = minted.split("::")
    assert (prefix, kind) == ("p1", "episodic")
    assert _UUID7_RE.fullmatch(suffix), suffix
    parsed = uuid.UUID(suffix)
    assert parsed.version == 7  # noqa: PLR2004
    assert parsed.variant == uuid.RFC_4122


def test_minted_ids_are_unique_across_a_burst() -> None:
    minted = {mint_chunk_id("p1", "episodic") for _ in range(1000)}
    assert len(minted) == 1000  # noqa: PLR2004 — the race fix: no shared counter


def test_recent_orders_mixed_old_and_minted_ids_by_created_at_not_id() -> None:
    # Bar (a) of K8-D-6, behavioural half: an old count-era id ("...::9999")
    # sorts lexicographically AFTER a minted id ("...::01…"), so any id-sort
    # would misorder them. recent() must order by created_at regardless.
    backend = _SpyBackend()
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    old_style = _chunk(make_chunk_id("p1", "episodic", 9999), created_at=_NOW)
    minted = _chunk(mint_chunk_id("p1", "episodic"), created_at=_NOW + timedelta(minutes=1))
    store.write("p1", [old_style])
    store.write("p1", [minted])

    got = store.recent("p1", limit=2)
    assert [c.id for c in got] == [minted.id, old_style.id]  # newest first, by created_at


def test_old_format_ids_remain_valid_alongside_minted_ones() -> None:
    # Bar (b): no reader assumes a format — both coexist in one store.
    backend = _SpyBackend()
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write("p1", [_chunk(make_chunk_id("p1", "episodic", 7))])
    store.write("p1", [_chunk(mint_chunk_id("p1", "episodic"))])
    assert len(store.get_all("p1")) == 2  # noqa: PLR2004
