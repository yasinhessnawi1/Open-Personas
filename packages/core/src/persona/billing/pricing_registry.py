"""The machine pricing registry — the code half of the one pricing truth (Spec M3, D-M3-5).

The runtime reads THIS (never the markdown). ``docs/pricing/pricing-table.md`` is
the human half; the two must stay in sync — the registry↔docs enumeration test
(``tests/unit/billing/test_pricing_sync.py``) fails if a registry row lacks a docs
row or vice-versa.

Scope of the registry:

* **Infra rates** — resolved from :class:`~persona.billing.formula.BillingConfig`
  by unit (:func:`infra_rate_cents`); the *only* runtime-authoritative number
  home for infra.
* **Per-surface enumeration** — one :class:`PricingRow` per paid surface
  (surface + provider + SKU + unit + representative basis + a documented
  provider-cost seed where a static estimate exists). The LLM per-model provider
  cost stays in the Spec-23 metadata resolver chain (``persona.backends.metadata``
  + ``compute_turn_cost``) — those rows carry ``provider_cost_cents=None`` because
  the resolver / OpenRouter actual is the truth, not a table constant here.

Seeds carry the D-M3-R1 verified numbers (⚠️ = confirm the live value at the
surface's wire-up task); they are documentation + the sync-test anchor, not the
per-call pricing path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from persona.billing.basis import (
    CostBasis,  # noqa: TC001 — Pydantic needs the field type at runtime
)

if TYPE_CHECKING:
    from persona.billing.formula import BillingConfig

__all__ = [
    "PRICING_ROWS",
    "InfraUnit",
    "PricingRow",
    "infra_rate_cents",
    "registry_keys",
]

#: Which infra flat rate applies to a surface's unit (``"none"`` = a provider
#: surface whose infra rides its per-call rate, or a meter with no infra add).
InfraUnit = Literal[
    "llm_call",
    "image",
    "voice_min",
    "embed_batch",
    "tool_call",
    "sandbox_exec",
    "none",
]


class PricingRow(BaseModel):
    """One paid surface's registry row (frozen; mirrors one docs-table row).

    Attributes:
        surface: The stable surface key (the sync-test join key with ``sku``).
        provider: The provider / service name (human label).
        sku: The model / SKU / unit name (the sync-test join key with ``surface``).
        unit: The billing unit (``1k tok`` / ``image`` / ``audio-min`` / ``min`` /
            ``batch`` / ``call`` / ``run``).
        provider_cost_cents: A documented static provider-cost seed in cents, or
            ``None`` when the real cost comes from the resolver chain / an
            OpenRouter actual (LLM surfaces).
        infra_unit: Which :class:`~persona.billing.formula.BillingConfig` infra
            rate is additive on this surface (:data:`InfraUnit`).
        cost_basis: The surface's representative ``cost_basis`` (the actual basis
            of a given row is decided at runtime for provider surfaces).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: str
    provider: str
    sku: str
    unit: str
    provider_cost_cents: float | None
    infra_unit: InfraUnit
    cost_basis: CostBasis


#: The seed registry (D-M3-R1). Enumeration must match ``docs/pricing/pricing-table.md``.
PRICING_ROWS: tuple[PricingRow, ...] = (
    # Floored surfaces (chat / authoring / image) carry infra VIA THE CREDIT FLOOR
    # (D-M3-4 amendment): the 1-credit (1¢) floor already covers per-call overhead
    # ~10× the 0.1¢ per-call infra rate, so ``infra_unit="none"`` — no separate
    # ``infra_flat`` add (which would double-count the floor's "minimum charge
    # covers overhead" logic and bump every sub-cent turn into the next ceil
    # bucket). The explicit per-call ``infra_flat`` applies to the unfloored
    # zero-provider surfaces below (T7). Enabling per-call infra on chat later is a
    # deliberate flip (set this to "llm_call" + pass the rate at the charge site).
    PricingRow(
        surface="chat",
        provider="openrouter/static",
        sku="per-model",
        unit="1k tok",
        provider_cost_cents=None,  # resolver chain / OpenRouter actual
        infra_unit="none",  # infra via the credit floor (D-M3-4 amendment)
        cost_basis="actual_openrouter",
    ),
    PricingRow(
        surface="authoring",
        provider="anthropic",
        sku="claude-sonnet-5",
        unit="1k tok",
        provider_cost_cents=None,  # static row lives in backends/metadata/anthropic.py (0.30/1.50)
        infra_unit="none",  # infra via the credit floor (D-M3-4 amendment)
        cost_basis="estimate_static",
    ),
    PricingRow(
        surface="image",
        provider="openrouter",
        sku="gpt-image-2",  # ⚠️ resolve the live OpenRouter slug at T3
        unit="image",
        provider_cost_cents=4.0,  # ⚠️ 1K standard catalog estimate; prefer usage.cost actual
        infra_unit="none",  # infra via the credit floor (D-M3-4 amendment)
        cost_basis="estimate_catalog",
    ),
    # Voice STT — the served provider is billed (Spec M3, T6a). Gladia is the V14
    # real-time primary; Deepgram is the fallback. Billed on REAL streamed audio
    # seconds (V8 rebase) at the ACTUALLY-SERVED provider's per-minute rate.
    PricingRow(
        surface="voice_stt",
        provider="gladia",
        sku="solaria-1",
        unit="audio-min",
        provider_cost_cents=1.25,  # ⚠️ Gladia real-time PAYG ≈1.25¢/min; on real streamed sec
        infra_unit="none",
        cost_basis="provider_meter",
    ),
    PricingRow(
        surface="voice_stt",
        provider="deepgram",
        sku="nova-3-streaming",
        unit="audio-min",
        provider_cost_cents=0.77,  # Nova-3 streaming PAYG (fallback); on real streamed sec
        infra_unit="none",
        cost_basis="provider_meter",
    ),
    # Voice TTS — ElevenLabs is the V14 primary, CHAR-metered (not per-min): Flash
    # ($0.05/1k char) + Multilingual ($0.10/1k char). Cartesia is the fallback
    # (char-metered ≈3¢/min; priced on the segment's audio minutes). Priced at the
    # served provider's real unit.
    PricingRow(
        surface="voice_tts",
        provider="elevenlabs",
        sku="eleven_flash_v2_5",
        unit="1k chars",
        provider_cost_cents=5.0,  # $0.05/1k chars (Flash, the V14 default model)
        infra_unit="none",
        cost_basis="provider_meter",
    ),
    PricingRow(
        surface="voice_tts",
        provider="elevenlabs",
        sku="eleven_multilingual_v2",
        unit="1k chars",
        provider_cost_cents=10.0,  # $0.10/1k chars (Multilingual quality tier)
        infra_unit="none",
        cost_basis="provider_meter",
    ),
    PricingRow(
        surface="voice_tts",
        provider="cartesia",
        sku="sonic",
        unit="min",
        provider_cost_cents=3.0,  # ⚠️ char-metered ≈3¢/min (fallback); on real audio min
        infra_unit="none",
        cost_basis="provider_meter",
    ),
    PricingRow(
        surface="voice_transport",
        provider="livekit",
        sku="self-host-fly",
        unit="min",
        provider_cost_cents=0.0,  # self-hosted → provider cost 0; infra-only
        infra_unit="voice_min",
        cost_basis="infra_flat",
    ),
    PricingRow(
        surface="embeddings",
        provider="local",
        sku="bge-small",
        unit="batch",
        provider_cost_cents=0.0,
        infra_unit="embed_batch",
        cost_basis="infra_flat",
    ),
    PricingRow(
        surface="connectors_mcp",
        provider="network",
        sku="call",
        unit="call",
        provider_cost_cents=0.0,
        infra_unit="tool_call",
        cost_basis="infra_flat",
    ),
    PricingRow(
        surface="sandbox",
        provider="local",
        sku="run",
        unit="run",
        provider_cost_cents=0.0,
        infra_unit="sandbox_exec",
        cost_basis="infra_flat",
    ),
)


def registry_keys() -> set[tuple[str, str]]:
    """The ``(surface, sku)`` join keys — what the registry↔docs sync test checks."""
    return {(row.surface, row.sku) for row in PRICING_ROWS}


def infra_rate_cents(config: BillingConfig, unit: InfraUnit) -> float:
    """The infra flat rate in cents for ``unit``, from the injected config.

    ``"none"`` → ``0.0`` (a provider surface with no additive infra, or a meter).
    Every other unit maps to its ``PERSONA_INFRA_RATE_*`` field on ``config``.
    """
    mapping: dict[InfraUnit, float] = {
        "llm_call": config.infra_rate_per_llm_call_cents,
        "image": config.infra_rate_per_image_cents,
        "voice_min": config.infra_rate_per_voice_min_cents,
        "embed_batch": config.infra_rate_per_embed_batch_cents,
        "tool_call": config.infra_rate_per_tool_call_cents,
        "sandbox_exec": config.infra_rate_per_sandbox_exec_cents,
        "none": 0.0,
    }
    return mapping[unit]
