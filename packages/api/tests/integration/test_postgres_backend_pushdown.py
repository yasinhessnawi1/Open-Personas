"""Spec K8 T1a/T1b — the widened Backend surface on the Postgres transport.

The SQL push-down half of the Liskov pair (Chroma sibling:
``packages/core/tests/integration/test_stores_backend_pushdown_chroma.py``):
``count`` is a real ``SELECT count(*)``, ``recent`` a real
``ORDER BY (created_at, id) DESC LIMIT``, ``get_by_logical_ids`` an indexed
``IN`` — plus the seeded-store scaling evidence for acceptance 1 (a write's
read-side work no longer grows with store size).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.schema.chunks import (
    ChunkProvenance,
    PersonaChunk,
    WriteSource,
    make_chunk_id,
    mint_chunk_id,
)
from persona.stores.episodic import EpisodicStore
from persona.stores.postgres import PostgresBackend
from persona.stores.self_facts import SelfFactsStore

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

UTC_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def backend(pg_engine: Engine, embedder: HashEmbedder384) -> PostgresBackend:
    from sqlalchemy import text

    with pg_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'u1@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1', 'u1', 'name: p1')")
        )
    return PostgresBackend(engine=pg_engine, embedder=embedder)


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


def test_count_distinguishes_all_versions_from_current_heads(backend: PostgresBackend) -> None:
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
    assert backend.count(persona_id="p1", store_kind="episodic") == 0


def test_recent_is_a_real_order_by_limit_newest_first(backend: PostgresBackend) -> None:
    older = _chunk(make_chunk_id("p1", "episodic", 9999), minutes=0)
    newer = _chunk(mint_chunk_id("p1", "episodic"), minutes=5)
    newest = _chunk(mint_chunk_id("p1", "episodic"), minutes=9)
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[newer, newest, older])

    got = backend.recent(persona_id="p1", store_kind="episodic", limit=2)
    # created_at decides — the old-format "9999" id would win a lexicographic
    # id-sort; it must not (K8-D-6 bar a, behavioural).
    assert [c.id for c in got] == [newest.id, newer.id]


def test_recent_excludes_superseded_versions(backend: PostgresBackend) -> None:
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
    assert [c.id for c in got] == [second.id]


def test_get_by_logical_ids_returns_full_chain_and_nothing_else(
    backend: PostgresBackend,
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
    store.write("p1", [_chunk("p1::self_facts::0009")], source=WriteSource.USER)

    chain = backend.get_by_logical_ids(
        persona_id="p1",
        store_kind="self_facts",
        logical_ids=[first.provenance.logical_id],  # type: ignore[union-attr]
    )
    assert {c.id for c in chain} == {first.id, second.id}
    assert (
        backend.get_by_logical_ids(persona_id="p1", store_kind="self_facts", logical_ids=[]) == []
    )


def test_write_read_work_is_flat_at_a_seeded_large_store(backend: PostgresBackend) -> None:
    """Acceptance 1 evidence: the write path's reads no longer scale with N.

    Seeds a large episodic store, then proves the K8 write path reads ZERO
    rows (fresh-chain write fetches only its own — empty — chain, id is
    minted) while the OLD pattern (``len(get_all)``) would have materialised
    every row. Row-shipping, not wall-clock, is the assertion — timing is
    machine-dependent; the row count is structural.
    """
    seeded = 2000
    batch: list[PersonaChunk] = []
    for i in range(seeded):
        batch.append(_chunk(mint_chunk_id("p1", "episodic"), minutes=i))
        if len(batch) == 500:  # noqa: PLR2004
            backend.upsert(persona_id="p1", store_kind="episodic", chunks=batch)
            batch = []
    if batch:
        backend.upsert(persona_id="p1", store_kind="episodic", chunks=batch)

    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())

    # The K8 write path: one fresh chunk against 2000 seeded rows.
    fresh = _chunk(mint_chunk_id("p1", "episodic"), minutes=seeded + 1)
    fetched = backend.get_by_logical_ids(
        persona_id="p1",
        store_kind="episodic",
        logical_ids=[fresh.provenance.logical_id],  # type: ignore[union-attr]
    )
    assert fetched == []  # the write's read-side work: zero rows at N=2000
    store.write("p1", [fresh])
    assert backend.count(persona_id="p1", store_kind="episodic", include_superseded=True) == (
        seeded + 1
    )

    # recent() ships exactly `limit` rows, not N.
    got = store.recent("p1", limit=5)
    assert len(got) == 5  # noqa: PLR2004
    assert got[0].id == fresh.id


# --- Spec K8 T2: lifecycle fields round-trip + the tamper-toggle guard ---------


def test_lifecycle_fields_round_trip_through_postgres(backend: PostgresBackend) -> None:
    raw = _chunk(mint_chunk_id("p1", "episodic")).model_copy(
        update={"strength": 4, "last_recalled_at": UTC_NOW, "pinned": True}
    )
    gist = PersonaChunk(
        id=mint_chunk_id("p1", "episodic_gist"),
        text="They discussed the Oslo move and the new job.",
        created_at=UTC_NOW,
        band=1,
        member_ids=(raw.id,),
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id="g-1",
            version=1,
            written_at=UTC_NOW,
            written_by="episodic.engine",
        ),
    )
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[raw])
    backend.upsert(persona_id="p1", store_kind="episodic_gist", chunks=[gist])

    got_raw = backend.get_all(persona_id="p1", store_kind="episodic")[0]
    assert got_raw.strength == 4  # noqa: PLR2004
    assert got_raw.last_recalled_at == UTC_NOW
    assert got_raw.pinned is True
    got_gist = backend.get_all(persona_id="p1", store_kind="episodic_gist")[0]
    assert got_gist.band == 1
    assert got_gist.member_ids == (raw.id,)  # TEXT[] round-trips ordered


def test_toggling_lifecycle_state_never_trips_the_tamper_check_on_postgres(
    backend: PostgresBackend,
) -> None:
    chunk = _chunk(mint_chunk_id("p1", "episodic"))
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[chunk])
    stored = backend.get_all(persona_id="p1", store_kind="episodic")[0]

    toggled = stored.model_copy(
        update={"strength": 7, "last_recalled_at": UTC_NOW, "band": 1, "pinned": True}
    )
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[toggled])
    got = backend.get_all(persona_id="p1", store_kind="episodic")[0]
    assert got.content_hash == chunk.content_hash  # identity untouched
    assert (got.strength, got.band, got.pinned) == (7, 1, True)


def test_reinforce_is_one_batched_update(backend: PostgresBackend) -> None:
    """K8-D-5 on Postgres: strength++ + clock set for the id-set; unknown ids skipped."""
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
    assert got[a.id].strength == 3  # noqa: PLR2004
    assert got[b.id].strength == 2  # noqa: PLR2004
    assert got[a.id].last_recalled_at == recalled_at
    assert got[a.id].content_hash == a.content_hash


def test_pyramid_round_trip_gist_drill_cascade_on_postgres(backend: PostgresBackend) -> None:
    """Spec K8 T4 end-to-end (§0) on the production transport."""
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

    store.remove_documents("p1", [a.id])
    assert store.pyramid.gists("p1") == []
    survivors = backend.get_all(persona_id="p1", store_kind="episodic")
    assert [c.id for c in survivors] == [b.id]
    assert survivors[0].content_hash == b.content_hash


def test_set_bands_and_histogram_round_trip_on_postgres(backend: PostgresBackend) -> None:
    """K8 T7: batched band UPDATE + GROUP BY histogram on the production transport."""
    a = _chunk(mint_chunk_id("p1", "episodic"))
    b = _chunk(mint_chunk_id("p1", "episodic"), minutes=1)
    backend.upsert(persona_id="p1", store_kind="episodic", chunks=[a, b])

    backend.set_bands(persona_id="p1", store_kind="episodic", bands={a.id: 1, "missing": 1})
    got = {c.id: c for c in backend.get_all(persona_id="p1", store_kind="episodic")}
    assert got[a.id].band == 1
    assert got[b.id].band == 0
    assert got[a.id].content_hash == a.content_hash
    assert backend.band_histogram(persona_id="p1", store_kind="episodic") == {0: 1, 1: 1}
