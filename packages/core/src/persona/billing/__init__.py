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
from persona.billing.plans import (
    DEFAULT_PLAN_CODE,
    PAYG_PACKS,
    PLANS,
    PaygPack,
    Plan,
    PlanCode,
    PlanModelSet,
    all_plans,
    credits_to_dollars,
    default_plan,
    dollars_to_credits,
    format_dollars,
    get_payg_pack,
    get_plan,
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
    "DEFAULT_PLAN_CODE",
    "PAYG_PACKS",
    "PLANS",
    "PRICING_ROWS",
    "BillingConfig",
    "ChargeMode",
    "ChargeResult",
    "CoreCreditsLedger",
    "CostBasis",
    "InfraUnit",
    "LedgerPort",
    "MeteredBilling",
    "PaygPack",
    "Plan",
    "PlanCode",
    "PlanModelSet",
    "PricingRow",
    "all_plans",
    "credits_charged",
    "credits_to_dollars",
    "default_plan",
    "dollars_to_credits",
    "format_dollars",
    "get_payg_pack",
    "get_plan",
    "infra_rate_cents",
    "livekit_infra_cents",
    "registry_keys",
    "voice_stt_cents",
    "voice_tts_cents",
]
