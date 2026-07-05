"""Spec K8 T1a/T1b — the widened Backend surface on the Chroma transport.

Round-trips ``count`` / ``recent`` / ``get_by_logical_ids`` against real
ChromaDB (the community/local transport). The Postgres sibling lives in
``packages/api/tests/integration/test_postgres_backend_pushdown.py`` — the two
files are the Liskov pair: same behavioural contract, different mechanics
(SQL push-down vs documented in-process materialisation).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 — used at runtime by pytest tmp_path

import pytest
from persona.audit import MemoryAuditLogger
from persona.schema.chunks import (
    ChunkProvenance,
    PersonaChunk,
    WriteSource,
    make_chunk_id,
    mint_chunk_id,
)
from persona.stores import ChromaBackend, SelfFactsStore

from tests._embedder import HashEmbedder

pytestmark = pytest.mark.integration

UTC_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def backend(tmp_path: Path) -> ChromaBackend:
    return ChromaBackend(persist_path=tmp_path / "chroma", embedder=HashEmbedder())


def _chunk(chunk_id: str, *, minutes: int = 0, logical_id: str | None = None) -> PersonaChunk:
    created = UTC_NOW + timedelta(minutes=minutes)
    return PersonaChunk(
        id=chunk_id,
        text=f"text of {chunk_id}",
        created_at=created,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id=logical_id or chunk_id,
            version=1,
            written_at=created,
            written_by="test",
        ),
    )


def test_count_counts_all_versions_and_current_heads_distinctly(
    backend: ChromaBackend,
) -> None:
    # Build a real superseded chain through the store (v1 -> v2 same logical id).
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    first = _chunk("p1::self_facts::0001")
    store.write("p1", [first], source=WriteSource.USER)
    second = _chunk(
        "p1::self_facts::0002",
        minutes=1,
        logical_id=first.provenance.logical_id,  # type: ignore[union-attr]
    )
    store.write("p1", [second], source=WriteSource.USER)

    assert backend.count(persona_id="p1", store_kind="self_facts", include_superseded=True) == 2  # noqa: PLR2004
    assert backend.count(persona_id="p1", store_kind="self_facts") == 1


def test_count_on_an_empty_store_is_zero(backend: ChromaBackend) -> None:
    assert backend.count(persona_id="p1", store_kind="episodic") == 0
    assert backend.count(persona_id="p1", store_kind="episodic", include_superseded=True) == 0


def test_recent_returns_newest_first_by_created_at_across_id_formats(
    backend: ChromaBackend,
) -> None:
    # Old count-era id vs minted uuidv7 id: created_at (not id) decides order.
    older = _chunk(make_chunk_id("p1", "episodic", 9999), minutes=0)
    newer = _chunk(mint_chunk_id("p1", "episodic"), minutes=5)
    newest = _chunk(mint_chunk_id("p1", "episodic"), minutes=9)
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[newer, newest, older])

    got = backend.recent(persona_id="p1", store_kind="episodic", limit=2)
    assert [c.id for c in got] == [newest.id, newer.id]


def test_recent_excludes_superseded_versions(backend: ChromaBackend) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    first = _chunk("p1::self_facts::0001")
    store.write("p1", [first], source=WriteSource.USER)
    second = _chunk(
        "p1::self_facts::0002",
        minutes=1,
        logical_id=first.provenance.logical_id,  # type: ignore[union-attr]
    )
    store.write("p1", [second], source=WriteSource.USER)

    got = backend.recent(persona_id="p1", store_kind="self_facts", limit=10)
    assert [c.id for c in got] == [second.id]  # the closed v1 never surfaces


def test_get_by_logical_ids_returns_the_full_chain_and_nothing_else(
    backend: ChromaBackend,
) -> None:
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    first = _chunk("p1::self_facts::0001")
    store.write("p1", [first], source=WriteSource.USER)
    second = _chunk(
        "p1::self_facts::0002",
        minutes=1,
        logical_id=first.provenance.logical_id,  # type: ignore[union-attr]
    )
    store.write("p1", [second], source=WriteSource.USER)
    unrelated = _chunk("p1::self_facts::0009")
    store.write("p1", [unrelated], source=WriteSource.USER)

    chain = backend.get_by_logical_ids(
        persona_id="p1",
        store_kind="self_facts",
        logical_ids=[first.provenance.logical_id],  # type: ignore[union-attr]
    )
    assert {c.id for c in chain} == {first.id, second.id}  # both versions, no strangers


def test_get_by_logical_ids_with_empty_input_returns_empty(backend: ChromaBackend) -> None:
    assert (
        backend.get_by_logical_ids(persona_id="p1", store_kind="self_facts", logical_ids=[]) == []
    )


# --- Spec K8 T2: lifecycle fields round-trip + the tamper-toggle guard ---------


def _gist(member_ids: tuple[str, ...]) -> PersonaChunk:
    return PersonaChunk(
        id=mint_chunk_id("p1", "episodic_gist"),
        text="They discussed the Oslo move and the new job.",
        created_at=UTC_NOW,
        band=1,
        member_ids=member_ids,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id="g-1",
            version=1,
            written_at=UTC_NOW,
            written_by="episodic.engine",
        ),
    )


def test_lifecycle_fields_round_trip_through_chroma(backend: ChromaBackend) -> None:
    raw = _chunk(mint_chunk_id("p1", "episodic")).model_copy(
        update={"strength": 4, "last_recalled_at": UTC_NOW, "pinned": True}
    )
    gist = _gist(member_ids=(raw.id,))
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[raw])
    backend.upsert(persona_id="p1", store_kind="episodic_gist", chunks=[gist])

    got_raw = backend.get_all(persona_id="p1", store_kind="episodic")[0]
    assert got_raw.strength == 4
    assert got_raw.last_recalled_at == UTC_NOW
    assert got_raw.pinned is True
    assert got_raw.band == 0
    got_gist = backend.get_all(persona_id="p1", store_kind="episodic_gist")[0]
    assert got_gist.band == 1
    assert got_gist.member_ids == (raw.id,)


def test_toggling_lifecycle_state_never_trips_the_tamper_check_on_chroma(
    backend: ChromaBackend,
) -> None:
    chunk = _chunk(mint_chunk_id("p1", "episodic"))
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[chunk])
    stored = backend.get_all(persona_id="p1", store_kind="episodic")[0]

    # Reinforce + demote + pin: lifecycle-only update, same identity.
    toggled = stored.model_copy(
        update={"strength": 7, "last_recalled_at": UTC_NOW, "band": 1, "pinned": True}
    )
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[toggled])
    # Read-back materialises through PersonaChunk validation — a hash drift
    # would raise "content_hash mismatch" right here.
    got = backend.get_all(persona_id="p1", store_kind="episodic")[0]
    assert got.content_hash == chunk.content_hash
    assert (got.strength, got.band, got.pinned) == (7, 1, True)


def test_reinforce_bumps_strength_and_clock_without_reembedding(
    backend: ChromaBackend,
) -> None:
    """K8-D-5 on Chroma: metadata-only bump; identity (hash) untouched; unknown ids skipped."""
    a = _chunk(mint_chunk_id("p1", "episodic"))
    b = _chunk(mint_chunk_id("p1", "episodic"), minutes=1)
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[a, b])

    recalled_at = UTC_NOW + timedelta(hours=2)
    backend.reinforce(
        persona_id="p1",
        store_kind="episodic",
        ids=[a.id, b.id, "p1::episodic::missing"],
        recalled_at=recalled_at,
    )
    backend.reinforce(persona_id="p1", store_kind="episodic", ids=[a.id], recalled_at=recalled_at)

    got = {c.id: c for c in backend.get_all(persona_id="p1", store_kind="episodic")}
    assert got[a.id].strength == 3  # 1 + two reinforcements
    assert got[b.id].strength == 2
    assert got[a.id].last_recalled_at == recalled_at
    assert got[a.id].content_hash == a.content_hash  # no identity churn, no tamper trip


def test_pyramid_round_trip_gist_drill_cascade_on_chroma(backend: ChromaBackend) -> None:
    """Spec K8 T4 end-to-end (§0): write gist → drill to untouched originals →
    privacy-delete a member → covering gist cascades away; raw survivor intact."""
    from persona.audit import MemoryAuditLogger
    from persona.stores.episodic import EpisodicStore

    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    a = _chunk(mint_chunk_id("p1", "episodic"), minutes=0)
    b = _chunk(mint_chunk_id("p1", "episodic"), minutes=1)
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[a, b])

    gist = store.pyramid.write_gist(
        "p1", text="a and b happened", member_ids=[a.id, b.id], created_at=UTC_NOW
    )
    drilled = store.pyramid.drill("p1", gist.id)
    assert [c.id for c in drilled] == [a.id, b.id]
    assert [c.content_hash for c in drilled] == [a.content_hash, b.content_hash]

    store.remove_documents("p1", [a.id])  # the privacy path
    assert store.pyramid.gists("p1") == []  # cascade: gist never outlives evidence
    survivors = backend.get_all(persona_id="p1", store_kind="episodic")
    assert [c.id for c in survivors] == [b.id]
    assert survivors[0].content_hash == b.content_hash  # survivor untouched


def test_set_bands_and_histogram_round_trip_on_chroma(backend: ChromaBackend) -> None:
    """K8 T7: band materialization is metadata-only; the histogram reads it back."""
    a = _chunk(mint_chunk_id("p1", "episodic"))
    b = _chunk(mint_chunk_id("p1", "episodic"), minutes=1)
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[a, b])

    backend.set_bands(persona_id="p1", store_kind="episodic", bands={a.id: 1, "missing": 1})
    got = {c.id: c for c in backend.get_all(persona_id="p1", store_kind="episodic")}
    assert got[a.id].band == 1
    assert got[b.id].band == 0
    assert got[a.id].content_hash == a.content_hash  # no identity churn
    assert backend.band_histogram(persona_id="p1", store_kind="episodic") == {0: 1, 1: 1}
