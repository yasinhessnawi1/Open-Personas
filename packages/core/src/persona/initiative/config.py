"""Env-driven configuration for initiative (Spec A5, T1; A5-D-X-thresholds-config).

Every restraint number is a setting, never hardcoded (the GraphSettings
discipline): the two-gate thresholds (value + acceptance) ship at the A5-R-1
swept operating point and re-tune as config; the cadence caps, interrupt
horizon, scan budget, and pool sizes are the bounded-work knobs (Phase-1
ruling 5 — bounds, not a parallel spend system).

``enabled`` is the criterion-9 gate: initiative registers nothing and fires
nothing until it is flipped, and it does not flip by default until the judged
quality gate passes.
"""

from __future__ import annotations

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["InitiativeSettings"]


class InitiativeSettings(BaseSettings):
    """Initiative tunables, read from ``PERSONA_INITIATIVE_*`` env vars.

    Attributes:
        enabled: The feature gate (criterion 9). Default OFF; nothing registers
            at the composition root while False.
        scan_hour: Local hour (user's tz, A8's resolution) the daily scan fires;
            the same fire flushes the held batch (A5-D-1, A5-D-X-flush-on-scan).
        scan_tier: The TierRegistry tier the scan routes on (ruling 1 — no
            router profile; the K2 ``synthesis_tier`` precedent).
        scan_max_input_tokens: Hard cap on the scan's assembled input (the
            bounded-work knob).
        scan_candidate_cap: Max candidates per scan, rank-ordered (≤3 — the
            research §6 hard cap; Pulse ships 5–10/day, we cap lower).
        recent_nodes_limit: The noticing-pool size read via
            ``GraphStore.recent_nodes`` (A5-D-X-reads).
        value_threshold: The fire floor on ``candidate.value`` — below it a
            candidate is silently not raised (a thin scan is success). Swept in
            A5-R-1; the shipped default is the chosen operating point.
        acceptance_floor: The independent SUPPRESSOR minimum on
            ``candidate.acceptance`` (A5-D-X-paccept-suppressor — gates down
            only; never trades off against value).
        daily_cap_per_persona: Max initiative contacts per persona per day (A5-D-3).
        weekly_cap_per_persona: Max per persona per rolling week.
        daily_cap_per_user: Max per USER per day across all personas (the
            pile-on ceiling above the per-persona caps).
        interrupt_horizon_hours: Hard-date window inside which a candidate may
            be ``INTERRUPT`` rather than ``BATCH`` — subject to quiet hours,
            which are ABSOLUTE (the Phase-3 polarity pin).
        hold_max_days: The hold-age bound (T7): a held notice older than this
            EXPIRES at flush (``suppressed_stale``, audited) — the hold is a
            buffer, not a queue that serves rotten items (the second Phase-3
            polarity pin, the age half).
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_INITIATIVE_", extra="ignore")

    enabled: bool = False
    scan_hour: int = Field(default=7, ge=0, le=23)
    scan_tier: str = Field(default="small", min_length=1)
    scan_max_input_tokens: int = Field(default=8000, gt=0)
    scan_candidate_cap: int = Field(default=3, gt=0)
    recent_nodes_limit: int = Field(default=30, gt=0)
    value_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    acceptance_floor: float = Field(default=0.5, ge=0.0, le=1.0)
    daily_cap_per_persona: int = Field(default=1, gt=0)
    weekly_cap_per_persona: int = Field(default=3, gt=0)
    daily_cap_per_user: int = Field(default=2, gt=0)
    interrupt_horizon_hours: int = Field(default=48, gt=0)
    hold_max_days: int = Field(default=7, gt=0)

    @model_validator(mode="after")
    def _caps_coherent(self) -> InitiativeSettings:
        """Fail fast on incoherent cap relationships (the boundary, not the tick)."""
        if self.weekly_cap_per_persona < self.daily_cap_per_persona:
            msg = "weekly_cap_per_persona must be >= daily_cap_per_persona"
            raise ValueError(msg)
        return self
