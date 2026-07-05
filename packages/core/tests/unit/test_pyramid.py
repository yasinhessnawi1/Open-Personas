"""Spec K8 T4 — the pyramid surface (K8-D-2/8/11/14; acceptance 2/3-structural/4).

The centerpiece (§0): drill-down bottoms out at the UNTOUCHED 100% original.
Proven here structurally, not asserted:

- the gist write path can only address the gist kind (spy: zero episodic-kind
  calls) — no pyramid path can mutate/delete a raw chunk's text or embedding;
- a gist may never be another gist's member (``GistMembershipError``) — no
  summary-of-summary can even be STORED;
- gist ids are deterministic (uuid5 over the sorted member set) — regeneration
  is idempotent by construction;
- the privacy cascade removes intersecting gists (derived artifacts never
  outlive their evidence) and the rebuild covers survivors only;
- band-resolved display renders the EXACT constant marker, dedupes sibling
  hits, fails soft to raw, and never mutates a stored row.
"""

# ruff: noqa: ARG002 — spy doubles ignore protocol args by design.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import GistMembershipError
from persona.schema.chunks import PersonaChunk, mint_chunk_id
from persona.stores.episodic import EpisodicStore
from persona.stores.pyramid import (
    GIST_KIND,
    OLDER_MEMORY_MARKER,
    EpisodicPyramid,
    make_gist_id,
    resolve_display,
)

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


class _SpyBackend:
    """In-memory Backend recording every (method, store_kind) touch."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, PersonaChunk]] = {}
        self.touches: list[tuple[str, str]] = []

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        self.touches.append(("upsert", store_kind))
        bucket = self.rows.setdefault((persona_id, store_kind), {})
        for c in chunks:
            bucket[c.id] = c

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        self.touches.append(("get_all", store_kind))
        return list(self.rows.get((persona_id, store_kind), {}).values())

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        self.touches.append(("get_by_logical_ids", store_kind))
        wanted = set(logical_ids)
        return [
            c
            for c in self.rows.get((persona_id, store_kind), {}).values()
            if c.provenance is not None and c.provenance.logical_id in wanted
        ]

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        self.touches.append(("delete_documents", store_kind))
        bucket = self.rows.get((persona_id, store_kind), {})
        for cid in ids:
            bucket.pop(cid, None)

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.touches.append(("delete_persona", store_kind))
        self.rows.pop((persona_id, store_kind), None)

    # Unused-by-pyramid Backend surface (present for EpisodicStore composition).
    def query(self, **_: object) -> list[PersonaChunk]:
        return []

    def count(self, *, persona_id: str, store_kind: str, include_superseded: bool = False) -> int:
        return len(self.rows.get((persona_id, store_kind), {}))

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        return []

    def reinforce(self, **_: object) -> None:
        return

    def set_bands(self, *, persona_id: str, store_kind: str, bands: dict[str, int]) -> None:
        self.touches.append(("set_bands", store_kind))
        bucket = self.rows.get((persona_id, store_kind), {})
        for cid, band in bands.items():
            if cid in bucket:
                bucket[cid] = bucket[cid].model_copy(update={"band": band})

    def band_histogram(self, *, persona_id: str, store_kind: str) -> dict[int, int]:
        return {}

    def write_raw(self, persona_id: str, chunk: PersonaChunk) -> None:
        self.rows.setdefault((persona_id, "episodic"), {})[chunk.id] = chunk


def _raw(cid: str | None = None, *, minutes: int = 0, band: int = 0) -> PersonaChunk:
    from persona.schema.chunks import ChunkProvenance, WriteSource

    chunk_id = cid or mint_chunk_id("p1", "episodic")
    created = _NOW + timedelta(minutes=minutes)
    return PersonaChunk(
        id=chunk_id,
        text=f"raw text of {chunk_id[-8:]}",
        created_at=created,
        band=band,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id=chunk_id,
            version=1,
            written_at=created,
            written_by="test",
        ),
    )


def _pyramid(backend: _SpyBackend | None = None) -> tuple[EpisodicPyramid, _SpyBackend]:
    b = backend or _SpyBackend()
    return EpisodicPyramid(backend=b, audit_logger=MemoryAuditLogger()), b


# --- the marker is EXACT and CONSTANT (K8-D-11 gate bar) ----------------------


def test_the_older_memory_marker_is_the_exact_constant() -> None:
    assert OLDER_MEMORY_MARKER == "[older memory — summarized]"


# --- gist identity (K8-D-8) ----------------------------------------------------


def test_gist_id_is_deterministic_and_order_insensitive() -> None:
    a = make_gist_id("p1", ["m1", "m2", "m3"])
    b = make_gist_id("p1", ["m3", "m1", "m2"])
    c = make_gist_id("p1", ["m1", "m2"])
    assert a == b  # same member SET ⇒ same identity (idempotent regen)
    assert a != c
    assert a.startswith(f"p1::{GIST_KIND}::")


# --- write_gist: structural §0 -------------------------------------------------


def test_write_gist_touches_only_the_gist_kind_never_raw_rows() -> None:
    pyramid, backend = _pyramid()
    raw = _raw()
    backend.write_raw("p1", raw)
    pyramid.write_gist("p1", text="a summary", member_ids=[raw.id], created_at=_NOW)

    episodic_mutations = [
        t for t in backend.touches if t[1] == "episodic" and t[0] != "get_by_logical_ids"
    ]
    assert episodic_mutations == []  # the raw base is structurally unreachable
    stored_raw = backend.rows[("p1", "episodic")][raw.id]
    assert stored_raw.text == raw.text
    assert stored_raw.content_hash == raw.content_hash  # untouched 100% original


def test_write_gist_rejects_a_gist_as_member() -> None:
    pyramid, _ = _pyramid()
    gist_id = make_gist_id("p1", ["m1"])
    with pytest.raises(GistMembershipError, match="from-originals"):
        pyramid.write_gist("p1", text="s", member_ids=[gist_id], created_at=_NOW)


def test_write_gist_rejects_empty_members_and_empty_text() -> None:
    pyramid, _ = _pyramid()
    with pytest.raises(GistMembershipError, match="at least one member"):
        pyramid.write_gist("p1", text="s", member_ids=[], created_at=_NOW)
    with pytest.raises(GistMembershipError, match="non-empty"):
        pyramid.write_gist("p1", text="   ", member_ids=["m1"], created_at=_NOW)


def test_write_gist_regeneration_is_idempotent() -> None:
    pyramid, backend = _pyramid()
    g1 = pyramid.write_gist("p1", text="summary v1", member_ids=["a", "b"], created_at=_NOW)
    g2 = pyramid.write_gist("p1", text="summary v2", member_ids=["b", "a"], created_at=_NOW)
    assert g1.id == g2.id  # same members ⇒ same identity ⇒ replace, not duplicate
    stored = backend.rows[("p1", GIST_KIND)]
    assert len(stored) == 1
    assert stored[g1.id].text == "summary v2"


def test_write_gist_is_audited_with_the_gist_store_kind() -> None:
    audit = MemoryAuditLogger()
    pyramid = EpisodicPyramid(backend=_SpyBackend(), audit_logger=audit)
    pyramid.write_gist("p1", text="s", member_ids=["a"], created_at=_NOW)
    assert [e.action for e in audit.events] == [AuditAction.WRITE]
    assert audit.events[0].store == GIST_KIND


# --- drill-down: bottoms out at the untouched original (§0 centerpiece) --------


def test_drill_returns_ordered_originals_with_hashes_intact() -> None:
    pyramid, backend = _pyramid()
    first, second = _raw(minutes=0), _raw(minutes=1)
    backend.write_raw("p1", first)
    backend.write_raw("p1", second)
    gist = pyramid.write_gist(
        "p1", text="both events", member_ids=[second.id, first.id], created_at=_NOW
    )

    drilled = pyramid.drill("p1", gist.id)
    assert [c.id for c in drilled] == [second.id, first.id]  # gist order, not time order
    assert [c.content_hash for c in drilled] == [second.content_hash, first.content_hash]
    assert drilled[0].text == second.text  # the 100% original, byte-for-byte


def test_drill_skips_privacy_deleted_members_without_error() -> None:
    pyramid, backend = _pyramid()
    kept, deleted = _raw(minutes=0), _raw(minutes=1)
    backend.write_raw("p1", kept)
    backend.write_raw("p1", deleted)
    gist = pyramid.write_gist("p1", text="s", member_ids=[kept.id, deleted.id], created_at=_NOW)
    del backend.rows[("p1", "episodic")][deleted.id]
    assert [c.id for c in pyramid.drill("p1", gist.id)] == [kept.id]


def test_drill_unknown_gist_returns_empty() -> None:
    pyramid, _ = _pyramid()
    assert pyramid.drill("p1", "p1::episodic_gist::nope") == []


# --- the privacy cascade (K8-D-14), both directions -----------------------------


def test_deleting_a_member_removes_the_covering_gist_and_reports_count() -> None:
    pyramid, backend = _pyramid()
    a, b, c = _raw(), _raw(minutes=1), _raw(minutes=2)
    for r in (a, b, c):
        backend.write_raw("p1", r)
    doomed = pyramid.write_gist("p1", text="ab", member_ids=[a.id, b.id], created_at=_NOW)
    survivor = pyramid.write_gist("p1", text="c", member_ids=[c.id], created_at=_NOW)

    removed = pyramid.remove_gists_for_members("p1", [a.id])
    assert removed == 1  # the honest report
    remaining = {g.id for g in pyramid.gists("p1")}
    assert remaining == {survivor.id}
    assert doomed.id not in remaining


def test_cascade_with_no_intersection_returns_zero_and_audits_nothing() -> None:
    audit = MemoryAuditLogger()
    backend = _SpyBackend()
    pyramid = EpisodicPyramid(backend=backend, audit_logger=audit)
    pyramid.write_gist("p1", text="s", member_ids=["a"], created_at=_NOW)
    events_before = len(audit.events)
    assert pyramid.remove_gists_for_members("p1", ["unrelated"]) == 0
    assert len(audit.events) == events_before  # zero is a valid answer, not a silent skip


def test_rebuild_after_cascade_covers_survivors_only() -> None:
    # The second direction: members updated on rebuild — the regenerated gist
    # has a NEW identity covering exactly the surviving originals.
    pyramid, backend = _pyramid()
    a, b = _raw(), _raw(minutes=1)
    backend.write_raw("p1", a)
    backend.write_raw("p1", b)
    old = pyramid.write_gist("p1", text="ab", member_ids=[a.id, b.id], created_at=_NOW)

    del backend.rows[("p1", "episodic")][a.id]  # the privacy delete of a's row
    pyramid.remove_gists_for_members("p1", [a.id])
    rebuilt = pyramid.write_gist("p1", text="b alone", member_ids=[b.id], created_at=_NOW)

    assert rebuilt.id != old.id
    assert rebuilt.member_ids == (b.id,)
    assert [c.id for c in pyramid.drill("p1", rebuilt.id)] == [b.id]


# --- EpisodicStore rides the sanctioned delete paths ----------------------------


def test_store_remove_documents_cascades_to_gists() -> None:
    backend = _SpyBackend()
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    raw = _raw()
    backend.write_raw("p1", raw)
    gist = store.pyramid.write_gist("p1", text="s", member_ids=[raw.id], created_at=_NOW)

    store.remove_documents("p1", [raw.id])
    assert backend.rows.get(("p1", "episodic"), {}) == {}
    assert gist.id not in backend.rows.get(("p1", GIST_KIND), {})


def test_store_delete_wipes_the_gist_layer_too() -> None:
    backend = _SpyBackend()
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    raw = _raw()
    backend.write_raw("p1", raw)
    store.pyramid.write_gist("p1", text="s", member_ids=[raw.id], created_at=_NOW)
    store.delete("p1")
    assert ("p1", GIST_KIND) not in backend.rows


# --- band-resolved display (K8-D-11) --------------------------------------------


def _gist_row(member_ids: tuple[str, ...], text: str = "the gist") -> PersonaChunk:
    return PersonaChunk(
        id=make_gist_id("p1", list(member_ids)),
        text=text,
        created_at=_NOW,
        band=1,
        member_ids=member_ids,
    )


def test_full_chunks_pass_through_untouched() -> None:
    raw = _raw()
    assert resolve_display([raw], covering={}) == [raw]


def test_demoted_chunk_renders_its_gist_with_the_exact_marker() -> None:
    demoted = _raw(band=1)
    gist = _gist_row((demoted.id,), text="They discussed the move.")
    out = resolve_display([demoted], covering={demoted.id: gist})
    assert len(out) == 1
    assert out[0].text == f"{OLDER_MEMORY_MARKER} They discussed the move."
    assert out[0].id == gist.id
    assert demoted.text.startswith("raw text")  # the stored row object untouched


def test_sibling_demoted_hits_dedupe_to_one_rendered_gist() -> None:
    d1, d2 = _raw(band=1), _raw(minutes=1, band=1)
    gist = _gist_row((d1.id, d2.id))
    out = resolve_display([d1, d2], covering={d1.id: gist, d2.id: gist})
    assert len(out) == 1
    assert out[0].id == gist.id


def test_demoted_chunk_with_no_gist_stays_raw_fail_soft() -> None:
    demoted = _raw(band=1)
    out = resolve_display([demoted], covering={})
    assert out == [demoted]  # never display nothing (K8-D-3)


def test_store_resolve_display_fetches_covering_gists() -> None:
    backend = _SpyBackend()
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    demoted = _raw(band=1)
    backend.write_raw("p1", demoted)
    gist = store.pyramid.write_gist(
        "p1", text="summary here", member_ids=[demoted.id], created_at=_NOW
    )
    out = store.resolve_display("p1", [demoted])
    assert out[0].id == gist.id
    assert out[0].text.startswith(OLDER_MEMORY_MARKER)


def test_store_resolve_display_with_no_demoted_hits_is_a_passthrough() -> None:
    backend = _SpyBackend()
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    raw = _raw()
    out = store.resolve_display("p1", [raw])
    assert out == [raw]
    assert ("get_all", GIST_KIND) not in backend.touches  # no gist fetch needed
