"""Spec K8 T7 — tier-and-demote bounding (K8-D-3/7; acceptance 6 unit half).

The band-materialization half of the engine pass: demote covered old chunks to
GIST display, promote reinforced/pinned ones back to FULL, never touch text or
embeddings, never delete. The bound (K8-D-7, restated at this task's close):
the FULL band is COUNT-denominated — tail ∪ young ∪ pinned ∪ retention
survivors — and the pass materializes exactly that set over covered chunks;
an uncovered chunk stays FULL regardless (never display nothing).
"""

# ruff: noqa: ARG002 — spy doubles ignore protocol args by design.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from persona.audit import MemoryAuditLogger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.engine import EpisodicConsolidationEngine
from persona.stores.lifecycle import EpisodicSettings
from persona.stores.pyramid import EpisodicPyramid
from persona.stores.summarizer import StubSummarizer

from tests.unit.test_episodic_engine import _RecordingMerge, _SpyBackend

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
# A tiny tail (2) + short age floor so the bounding structure is visible at
# unit scale; min_chunks high enough that no NEW windows form in these tests
# (tiering is exercised in isolation from gist building).
_SETTINGS = EpisodicSettings(
    tau0_hours=168.0,
    demote_threshold=0.05,
    verbatim_tail_count=2,
    min_verbatim_days=1.0,
    pin_threshold=0.8,
    min_chunks_per_run=99,
    cluster_gap_minutes=45.0,
)


def _chunk(
    *,
    days_ago: float,
    band: int = 0,
    pinned: bool = False,
    strength: int = 1,
    last_recalled_days_ago: float | None = None,
) -> PersonaChunk:
    cid = mint_chunk_id("p1", "episodic")
    created = _NOW - timedelta(days=days_ago)
    return PersonaChunk(
        id=cid,
        text=f"raw {cid[-6:]}",
        created_at=created,
        band=band,
        pinned=pinned,
        strength=strength,
        last_recalled_at=(
            _NOW - timedelta(days=last_recalled_days_ago)
            if last_recalled_days_ago is not None
            else None
        ),
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id=cid,
            version=1,
            written_at=created,
            written_by="test",
        ),
    )


def _engine(backend: _SpyBackend) -> tuple[EpisodicConsolidationEngine, EpisodicPyramid]:
    pyramid = EpisodicPyramid(backend=backend, audit_logger=MemoryAuditLogger())
    engine = EpisodicConsolidationEngine(
        backend=backend,
        pyramid=pyramid,
        summarizer=StubSummarizer(),
        graph=_RecordingMerge(),
        settings=_SETTINGS,
    )
    return engine, pyramid


def _cover(pyramid: EpisodicPyramid, chunks: list[PersonaChunk]) -> None:
    pyramid.write_gist(
        "p1",
        text="covering gist",
        member_ids=[c.id for c in chunks],
        created_at=_NOW,
    )


def test_covered_old_chunks_demote_and_the_full_band_is_the_bound_set() -> None:
    """Acceptance 6 (unit half): FULL = tail ∪ young ∪ pinned ∪ strong; nothing deleted."""
    backend = _SpyBackend()
    old_plain = [_chunk(days_ago=90 + i) for i in range(6)]
    old_pinned = _chunk(days_ago=95, pinned=True)
    old_strong = _chunk(days_ago=95, strength=40, last_recalled_days_ago=2)
    young = _chunk(days_ago=0.5)
    tail_a, tail_b = _chunk(days_ago=0.1), _chunk(days_ago=0.2)  # the 2 newest
    everything = [*old_plain, old_pinned, old_strong, young, tail_a, tail_b]
    for c in everything:
        backend.seed_raw("p1", c)

    engine, pyramid = _engine(backend)
    _cover(pyramid, [*old_plain, old_pinned, old_strong])  # all old chunks covered

    report = asyncio.run(engine.run("u1", "p1", now=_NOW))

    rows = backend.rows[("p1", "episodic")]
    demoted_ids = {cid for cid, c in rows.items() if c.band == 1}
    assert demoted_ids == {c.id for c in old_plain}  # ONLY the unpinned/unrecalled old
    assert rows[old_pinned.id].band == 0  # pinned never compresses (AFM)
    assert rows[old_strong.id].band == 0  # reinforcement holds the band
    assert rows[young.id].band == 0  # age floor
    assert rows[tail_a.id].band == 0
    assert rows[tail_b.id].band == 0  # verbatim tail
    assert report.bands_demoted == len(old_plain)
    assert report.full_band_size == len(everything) - len(old_plain)
    assert len(rows) == len(everything)  # NOTHING deleted (tier-and-demote)
    assert all(c.text.startswith("raw ") for c in rows.values())  # text untouched


def test_uncovered_old_chunks_stay_full_never_display_nothing() -> None:
    backend = _SpyBackend()
    uncovered_old = _chunk(days_ago=120)
    backend.seed_raw("p1", uncovered_old)
    engine, _ = _engine(backend)

    report = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert backend.rows[("p1", "episodic")][uncovered_old.id].band == 0
    assert report.bands_demoted == 0


def test_reinforced_demoted_chunk_promotes_back_to_full() -> None:
    # The re-promotion half of K8-D-3: a demoted chunk recalled since (strength
    # up, clock reset) is materialized BACK to FULL by the next pass.
    backend = _SpyBackend()
    reinforced = _chunk(days_ago=90, band=1, strength=30, last_recalled_days_ago=0.5)
    backend.seed_raw("p1", reinforced)
    engine, pyramid = _engine(backend)
    _cover(pyramid, [reinforced])

    report = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert backend.rows[("p1", "episodic")][reinforced.id].band == 0
    assert report.bands_promoted == 1


def test_tiering_is_idempotent_second_pass_writes_nothing() -> None:
    backend = _SpyBackend()
    old = [_chunk(days_ago=90 + i) for i in range(3)]
    tail = [_chunk(days_ago=0.1), _chunk(days_ago=0.2)]  # occupy the 2-slot tail
    for c in [*old, *tail]:
        backend.seed_raw("p1", c)
    engine, pyramid = _engine(backend)
    _cover(pyramid, old)

    first = asyncio.run(engine.run("u1", "p1", now=_NOW))
    set_bands_calls_after_first = [t for t in backend.touches if t[0] == "set_bands"]
    second = asyncio.run(engine.run("u1", "p1", now=_NOW))
    set_bands_calls_after_second = [t for t in backend.touches if t[0] == "set_bands"]

    assert first.bands_demoted == 3  # noqa: PLR2004
    assert second.bands_demoted == 0
    assert second.bands_promoted == 0
    assert len(set_bands_calls_after_second) == len(set_bands_calls_after_first)  # no write


def test_band_flips_never_change_identity() -> None:
    backend = _SpyBackend()
    old = _chunk(days_ago=90)
    tail = [_chunk(days_ago=0.1), _chunk(days_ago=0.2)]  # keep old out of the tail
    for c in [old, *tail]:
        backend.seed_raw("p1", c)
    engine, pyramid = _engine(backend)
    _cover(pyramid, [old])

    asyncio.run(engine.run("u1", "p1", now=_NOW))
    after = backend.rows[("p1", "episodic")][old.id]
    assert after.band == 1
    assert after.content_hash == old.content_hash  # lifecycle-only, no identity churn
    assert after.text == old.text
