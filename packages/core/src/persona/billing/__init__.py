"""The M3 billing seam — one credit formula, one pricing registry, one seam.

Lives in persona-core over ``persona.credits`` (D-M3-core-seam): persona-voice
imports it directly (latency-critical, no persona-api hop) and persona-api
injects a ``CreditsPolicy``-backed adapter around it. See ``docs/decisions/spec_M3.md``.
"""

from __future__ import annotations

from persona.billing.basis import CostBasis
from persona.billing.formula import BillingConfig, credits_charged
from persona.billing.metered import (
    ChargeMode,
    ChargeResult,
    CoreCreditsLedger,
    LedgerPort,
    MeteredBilling,
)
from persona.billing.pricing_registry import (
    PRICING_ROWS,
    InfraUnit,
    PricingRow,
    infra_rate_cents,
    registry_keys,
)
from persona.billing.voice_pricing import (
    livekit_infra_cents,
    voice_stt_cents,
    voice_tts_cents,
)

__all__ = [
    "PRICING_ROWS",
    "BillingConfig",
    "ChargeMode",
    "ChargeResult",
    "CoreCreditsLedger",
    "CostBasis",
    "InfraUnit",
    "LedgerPort",
    "MeteredBilling",
    "PricingRow",
    "credits_charged",
    "infra_rate_cents",
    "livekit_infra_cents",
    "registry_keys",
    "voice_stt_cents",
    "voice_tts_cents",
]
