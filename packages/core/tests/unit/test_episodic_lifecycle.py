"""Spec K8 T3 — decay made structural (K8-D-3/4/5; acceptance 5).

Pins the four T3 bars:

1. **Per-kind rates via EpisodicSettings** — env-tunable, no magic numbers.
2. **The pure band classifier** — band derived from the K8-D-3 inputs
   (tail / age floor / pin / retention), wall-clock-free (``now`` injected).
3. **reinforce() is an explicit command** — MemoryBank's rule (strength++ +
   clock reset), ONE batched backend call, audited.
4. **The flat-24h path is GONE** — structural attribute proof + the
   behavioural halves: divergent rates actually diverge (pinned vs volatile,
   recalled vs unrecalled) and reinforcement measurably moves band/strength;
   the ranking floor means old memory is never ranking-dead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.schema.chunks import PersonaChunk, mint_chunk_id
from persona.stores.episodic import EpisodicStore
from persona.stores.lifecycle import (
    BAND_FULL,
    BAND_GIST,
    EpisodicSettings,
    classify_band,
    is_pinned,
    retention,
)

# Anchored to the real clock, NOT a frozen literal. `EpisodicStore.query()`
# scores with `datetime.now(UTC)`, so a hardcoded _NOW makes every chunk age by
# one real day per real day. This test file was written with _NOW frozen at
# 2026-07-04; by 2026-09-02 the "20 day old" chunks in
# test_recalled_memory_outranks_equally_similar_unrecalled were ~80 real days
# old, both retentions had fallen under `ranking_floor`, `max(r, floor)`
# clamped them to the SAME value, and the stable sort returned them in input
# order, so the reinforced chunk lost. A time bomb, not a flake: once the
# calendar passed the floor boundary it failed every run.
#
# Ages here are relative to _NOW, and the assertions that pass `now=_NOW`
# explicitly stay exact, so tracking the real clock keeps every case honest.
_NOW = datetime.now(UTC)
_SETTINGS = EpisodicSettings(
    tau0_hours=168.0,
    demote_threshold=0.05,
    ranking_floor=0.1,
    verbatim_tail_count=2,
    min_verbatim_days=14.0,
    pin_threshold=0.8,
)


def _chunk(
    *,
    age_days: float = 0.0,
    strength: int = 1,
    pinned: bool = False,
    importance: str | None = "0.5",
    last_recalled_days_ago: float | None = None,
    distance: float | None = None,
) -> PersonaChunk:
    meta = {} if importance is None else {"importance": importance}
    return PersonaChunk(
        id=mint_chunk_id("p1", "episodic"),
        text="a memory",
        metadata=meta,
        created_at=_NOW - timedelta(days=age_days),
        strength=strength,
        pinned=pinned,
        last_recalled_at=(
            _NOW - timedelta(days=last_recalled_days_ago)
            if last_recalled_days_ago is not None
            else None
        ),
        distance=distance,
    )


# --- 1. settings: env-tunable, validated --------------------------------------


def test_settings_defaults_are_the_phase3_numbers() -> None:
    s = EpisodicSettings()
    assert (s.tau0_hours, s.demote_threshold, s.ranking_floor) == (168.0, 0.05, 0.1)
    assert (s.verbatim_tail_count, s.min_verbatim_days, s.pin_threshold) == (200, 14.0, 0.8)


def test_settings_read_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EPISODIC_TAU0_HOURS", "24")
    monkeypatch.setenv("PERSONA_EPISODIC_PIN_THRESHOLD", "0.9")
    s = EpisodicSettings()
    assert s.tau0_hours == 24.0  # noqa: PLR2004
    assert s.pin_threshold == 0.9  # noqa: PLR2004


def test_settings_reject_nonpositive_tau(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EPISODIC_TAU0_HOURS", "0")
    with pytest.raises(ValueError, match="tau0_hours"):
        EpisodicSettings()


# --- 2. the pure classifier (wall-clock-free) ----------------------------------


def test_retention_decays_with_age_and_strength_extends_it() -> None:
    fresh = retention(_chunk(age_days=0), now=_NOW, settings=_SETTINGS)
    month_old = retention(_chunk(age_days=30), now=_NOW, settings=_SETTINGS)
    month_old_recalled = retention(_chunk(age_days=30, strength=5), now=_NOW, settings=_SETTINGS)
    assert fresh == pytest.approx(1.0)
    assert month_old < 0.05  # noqa: PLR2004 — unrecalled month ⇒ below demote bar
    assert month_old_recalled > month_old  # divergent per-kind rates DIVERGE


def test_recall_resets_the_decay_clock() -> None:
    stale = _chunk(age_days=60)
    recalled_yesterday = _chunk(age_days=60, last_recalled_days_ago=1)
    assert retention(recalled_yesterday, now=_NOW, settings=_SETTINGS) > retention(
        stale, now=_NOW, settings=_SETTINGS
    )


def test_pin_class_flag_and_importance_threshold() -> None:
    assert is_pinned(_chunk(pinned=True), settings=_SETTINGS)
    assert is_pinned(_chunk(importance="0.9"), settings=_SETTINGS)
    assert not is_pinned(_chunk(importance="0.5"), settings=_SETTINGS)
    assert not is_pinned(_chunk(importance=None), settings=_SETTINGS)
    assert not is_pinned(_chunk(importance="not-a-number"), settings=_SETTINGS)


def test_classify_band_tail_age_pin_and_retention_gates() -> None:
    old = 90.0  # far past every time gate at strength 1
    assert classify_band(_chunk(age_days=old), now=_NOW, settings=_SETTINGS, tail_rank=0) == (
        BAND_FULL
    )  # verbatim tail
    assert classify_band(_chunk(age_days=1.0), now=_NOW, settings=_SETTINGS) == BAND_FULL  # age
    assert (
        classify_band(_chunk(age_days=old, pinned=True), now=_NOW, settings=_SETTINGS) == BAND_FULL
    )  # pinned NEVER demotes (the AFM rule)
    assert (
        classify_band(_chunk(age_days=old, strength=20), now=_NOW, settings=_SETTINGS) == BAND_FULL
    )  # reinforced retention clears the bar
    assert classify_band(_chunk(age_days=old), now=_NOW, settings=_SETTINGS) == BAND_GIST


def test_reinforcement_measurably_moves_the_band() -> None:
    # The acceptance-5 sentence: recall reinforcement resets/raises band position.
    demoted = _chunk(age_days=90)
    assert classify_band(demoted, now=_NOW, settings=_SETTINGS) == BAND_GIST
    reinforced = demoted.model_copy(update={"strength": 2, "last_recalled_at": _NOW})
    assert classify_band(reinforced, now=_NOW, settings=_SETTINGS) == BAND_FULL


# --- 3. ranking: the floor kills the ranking-dead bug class --------------------


class _QueryBackend:
    """Serves a fixed candidate list to EpisodicStore.query."""

    def __init__(self, chunks: list[PersonaChunk]) -> None:
        self._chunks = chunks

    def query(self, **_: object) -> list[PersonaChunk]:
        return list(self._chunks)


def test_old_but_relevant_memory_is_never_ranking_dead() -> None:
    # A year-old high-similarity memory must outrank a fresh barely-relevant
    # one: sim 0.9 · floor 0.1 = 0.09 > sim 0.05 · 1.0 = 0.05. Under the old
    # flat-24h decay the year-old chunk scored ~0 (the bug this kills).
    year_old_relevant = _chunk(age_days=365, distance=0.1)
    fresh_irrelevant = _chunk(age_days=0, distance=0.95)
    store = EpisodicStore(
        backend=_QueryBackend([fresh_irrelevant, year_old_relevant]),
        audit_logger=MemoryAuditLogger(),
        settings=_SETTINGS,
    )
    got = store.query("p1", "anything", top_k=1)
    assert got[0].id == year_old_relevant.id


def test_recalled_memory_outranks_equally_similar_unrecalled() -> None:
    # Same similarity, same age — the reinforced one wins (usage-reinforced,
    # not flat). Ages inside the floor-free zone so retention differs.
    unrecalled = _chunk(age_days=20, distance=0.3)
    recalled = _chunk(age_days=20, strength=4, distance=0.3)
    store = EpisodicStore(
        backend=_QueryBackend([unrecalled, recalled]),
        audit_logger=MemoryAuditLogger(),
        settings=_SETTINGS,
    )
    got = store.query("p1", "anything", top_k=2)
    assert got[0].id == recalled.id


# --- 4. reinforce(): explicit, batched, audited --------------------------------


class _ReinforceSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[str]]] = []

    def reinforce(
        self, *, persona_id: str, store_kind: str, ids: list[str], recalled_at: datetime
    ) -> None:
        assert recalled_at.tzinfo is not None
        self.calls.append((persona_id, store_kind, ids))


def test_reinforce_is_one_batched_backend_call_and_audited() -> None:
    backend = _ReinforceSpy()
    audit = MemoryAuditLogger()
    store = EpisodicStore(backend=backend, audit_logger=audit, settings=_SETTINGS)
    store.reinforce("p1", ["a", "b", "c"])
    assert backend.calls == [("p1", "episodic", ["a", "b", "c"])]  # ONE batch
    assert [e.action for e in audit.events] == [AuditAction.REINFORCE]
    assert audit.events[0].chunk_ids == ["a", "b", "c"]


def test_reinforce_with_no_ids_is_a_noop() -> None:
    backend = _ReinforceSpy()
    audit = MemoryAuditLogger()
    store = EpisodicStore(backend=backend, audit_logger=audit, settings=_SETTINGS)
    store.reinforce("p1", [])
    assert backend.calls == []
    assert audit.events == []


# --- 5. the flat-24h path is GONE (structural) ----------------------------------


def test_flat_decay_surface_is_gone_from_the_store() -> None:
    import persona.stores.episodic as module

    assert not hasattr(module.EpisodicStore, "DEFAULT_TAU_HOURS")
    assert not hasattr(module.EpisodicStore, "tau_hours")
    store = EpisodicStore(backend=_QueryBackend([]), audit_logger=MemoryAuditLogger())
    assert not hasattr(store, "_tau_hours")
    assert isinstance(store.settings, EpisodicSettings)
