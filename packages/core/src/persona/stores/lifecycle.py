"""Episodic lifecycle policy — settings + the pure band/retention functions (Spec K8).

Decay made structural (K8-D-3/4, as amended at T2): retention is MemoryBank's
``R = e^(−Δt/S)`` with S = ``tau0_hours · strength`` (recall extends linearly),
and the fidelity band is a pure function of the K8-D-3 inputs — verbatim tail,
age floor, pin class, retention. The band the read path sees is the value the
background tiering pass MATERIALIZED from this function; staleness between
passes is harmless (display fidelity, never correctness).

Everything here is pure and wall-clock-free (``now`` is always injected — the
K7 no-wallclock discipline): the store's ranking, the tiering pass, and the
tests all call the same functions with an explicit ``now``.

No magic numbers: every constant is an :class:`EpisodicSettings` field,
env-tunable via ``PERSONA_EPISODIC_*`` (the GraphSettings precedent). Defaults
are the Phase-3 starting points; P8 calibrates them against real accumulation.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from datetime import datetime

    from persona.schema.chunks import PersonaChunk

__all__ = [
    "BAND_FULL",
    "BAND_GIST",
    "EpisodicSettings",
    "classify_band",
    "is_pinned",
    "retention",
]

#: Band 0 — the chunk's raw text is displayed (FULL fidelity).
BAND_FULL: int = 0
#: Band 1 — the covering gist's text is displayed (K8-D-11). Additional bands
#: are data, not DDL (K8-D-1): a future B-gradient adds values, not columns.
BAND_GIST: int = 1


class EpisodicSettings(BaseSettings):
    """Episodic lifecycle tunables, read from ``PERSONA_EPISODIC_*`` env vars.

    Attributes:
        tau0_hours: Base retention time-constant. Effective S = tau0 · strength
            (MemoryBank: each recall extends the memory's lifetime linearly).
        demote_threshold: A chunk stays FULL while ``retention >= this``
            (default 0.05 ≈ 3 unrecalled weeks at strength 1, tau0 168h).
        ranking_floor: Retention floor in query ranking (K8-D-4) — decay
            down-ranks but can never rank-kill; old-but-relevant memory stays
            findable (the flat-24h "ranking-dead" bug class).
        verbatim_tail_count: The N most recent chunks always displayed FULL
            regardless of age (K8-D-7: this tail is part of the FULL-band bound).
        min_verbatim_days: Chunks younger than this never demote (age floor).
        pin_threshold: Write-time importance at or above this pins the chunk
            (the constraint class, with the explicit ``pinned`` flag; K8-D-3 as
            amended — pins never compress, the AFM rule).
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_EPISODIC_", extra="ignore")

    tau0_hours: float = Field(default=168.0, gt=0)
    demote_threshold: float = Field(default=0.05, ge=0.0, le=1.0)
    ranking_floor: float = Field(default=0.1, ge=0.0, le=1.0)
    verbatim_tail_count: int = Field(default=200, ge=0)
    min_verbatim_days: float = Field(default=14.0, ge=0)
    pin_threshold: float = Field(default=0.8, ge=0.0, le=1.0)

    # --- the sleep-time engine (T6, K8-D-8) ---------------------------------
    # Windows: a session boundary is a silence longer than cluster_gap_minutes;
    # a topic split inside a window fires when adjacent-chunk similarity dips
    # below cluster_split_threshold (bge band); windows are size-capped.
    min_chunks_per_run: int = Field(default=5, ge=1)
    cluster_gap_minutes: float = Field(default=45.0, gt=0)
    cluster_split_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    max_cluster_chunks: int = Field(default=40, ge=1)
    gist_target_tokens: int = Field(default=120, ge=8)
    engine_max_batch: int = Field(default=500, ge=1)
    # Cadence + the kill switch (K8-D-8): the turn-tail trigger defers runs to
    # the idle boundary and coalesces bursts per bucket; engine_enabled gates
    # BOTH the handler registration and the trigger (built-but-inert guard).
    engine_enabled: bool = Field(default=True)
    idle_delay_seconds: float = Field(default=900.0, ge=0)
    bucket_seconds: float = Field(default=3600.0, gt=0)


def retention(chunk: PersonaChunk, *, now: datetime, settings: EpisodicSettings) -> float:
    """MemoryBank retention ``R = exp(−Δt / (tau0 · strength))`` in ``(0, 1]``.

    Δt counts from the chunk's last recall (``last_recalled_at``), falling back
    to ``created_at`` — recall resets the clock (K8-D-5); ``strength`` scales
    the time-constant so a reinforced memory decays slower (usage-reinforced,
    never flat).
    """
    anchor = chunk.last_recalled_at or chunk.created_at
    elapsed_h = max(0.0, (now - anchor).total_seconds() / 3600.0)
    return math.exp(-elapsed_h / (settings.tau0_hours * max(1, chunk.strength)))


def is_pinned(chunk: PersonaChunk, *, settings: EpisodicSettings) -> bool:
    """The chunk-level constraint class (K8-D-3 as amended at T2).

    Pinned = the explicit hash-excluded ``pinned`` flag OR write-time
    ``metadata["importance"] >= pin_threshold``. Importance is write-once
    metadata (hash-stable); a malformed/absent value reads as not-pinned
    (fail-safe toward the decay path, never toward an error).
    """
    if chunk.pinned:
        return True
    raw = chunk.metadata.get("importance")
    if raw is None:
        return False
    try:
        return float(raw) >= settings.pin_threshold
    except ValueError:
        return False


def classify_band(
    chunk: PersonaChunk,
    *,
    now: datetime,
    settings: EpisodicSettings,
    tail_rank: int | None = None,
) -> int:
    """The K8-D-3 band function — what the tiering pass materializes.

    A raw chunk is FULL (band 0) iff ANY of: it sits inside the verbatim tail
    (``tail_rank`` = its 0-based recency rank, ``None`` = unknown ⇒ not tail);
    it is younger than the age floor; it is pinned; its retention clears the
    demote threshold. Otherwise GIST (band 1) — and the DISPLAY resolution
    falls back to raw anyway while no covering gist exists (fail-soft,
    K8-D-3: never display nothing).
    """
    if tail_rank is not None and tail_rank < settings.verbatim_tail_count:
        return BAND_FULL
    age_h = max(0.0, (now - chunk.created_at).total_seconds() / 3600.0)
    if age_h < settings.min_verbatim_days * 24.0:
        return BAND_FULL
    if is_pinned(chunk, settings=settings):
        return BAND_FULL
    if retention(chunk, now=now, settings=settings) >= settings.demote_threshold:
        return BAND_FULL
    return BAND_GIST
