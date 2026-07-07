"""Env-driven configuration for event triggers (Spec A7, A7-D-4).

Every storm/loop number is a setting, never hardcoded (the InitiativeSettings / GraphSettings
discipline): the coalescing window, the provenance-chain depth cap, and the per-owner ceiling are
the bounded-work knobs. The **budget cap is R7's** (A7-D-8), consulted before every enqueue — A7
adds no parallel spend system.

``enabled`` is the feature gate (the A5 criterion-9 posture): nothing registers at the composition
root and no event fires while it is False, and it does not flip by default.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["EventTriggerSettings"]


class EventTriggerSettings(BaseSettings):
    """Event-trigger tunables, read from ``PERSONA_EVENT_TRIGGERS_*`` env vars.

    Attributes:
        enabled: The feature gate. Default OFF; the dispatcher registers nothing and matches
            nothing while False (the composition-root branch A7's built-but-inert test covers).
        cooldown_seconds: The per-trigger coalescing window (A7-D-4). Within it, a burst coalesces
            into ONE fire carrying the coalesced count; the rest are dropped-with-audit.
        max_chain_depth: The provenance-chain depth cap (the loop "iteration cap", research §4). A
            match whose chain already contains the trigger is refused regardless; this backstops a
            cycle threading DISTINCT triggers.
        per_owner_max_fires_per_hour: The per-owner ceiling above the per-trigger cooldown (the
            pile-on guard across many triggers); over-cap ⇒ drop-with-audit + surfaced.
        fire_cost_estimate: The nominal per-fire cost booked against R7's day-cap at the pre-enqueue
            consult (A7-D-8) — the storm money guard, not a real spend (the real leg meters
            downstream). ``0`` disables the day-cap pre-check (concurrency still consulted).
    """

    model_config = SettingsConfigDict(env_prefix="PERSONA_EVENT_TRIGGERS_", extra="ignore")

    enabled: bool = False
    cooldown_seconds: int = Field(default=300, gt=0)
    max_chain_depth: int = Field(default=3, gt=0)
    per_owner_max_fires_per_hour: int = Field(default=60, gt=0)
    fire_cost_estimate: int = Field(default=1, ge=0)
