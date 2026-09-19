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
    "served_row",
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
    # The other image backends report no cost of their own, so THIS row is what they
    # are billed. Each price below is the vendor's own published list price for the
    # request this product actually makes: size 1024x1024 and quality "standard" (the
    # ImageGenOptions defaults, and what the avatar path hard-codes). The page and the
    # date it was read are in the row's comment so the next person can re-check it
    # without repeating the research. Where two readings of a provider's rule were
    # possible, the row takes the LOWER one: an under-priced row costs the house money,
    # an over-priced one takes a person's.
    PricingRow(
        surface="image",
        provider="openai",
        sku="gpt-image-1",
        unit="image",
        # $0.042 for 1024x1024 at medium quality, from the per-image table in OpenAI's
        # image-generation guide ("Calculating costs"), read 2026-09-19 at
        # https://developers.openai.com/api/docs/guides/image-generation. The neutral
        # quality "standard" maps to OpenAI "medium" at the wire (_OPENAI_QUALITY_MAPPING
        # in persona.imagegen.openai_image), so medium is the tier this product buys.
        # The prompt's own text tokens ($5 per Mtok) are a rounding error next to it and
        # are not added here.
        provider_cost_cents=4.2,
        infra_unit="none",  # infra via the credit floor (D-M3-4 amendment)
        cost_basis="estimate_static",
    ),
    PricingRow(
        surface="image",
        provider="fal",
        sku="fal-ai/flux-pro/v1.1",
        unit="image",
        # "$0.04 per megapixel", with "Images are billed by rounding up to the nearest
        # megapixel", from the model's own page, read 2026-09-19 at
        # https://fal.ai/models/fal-ai/flux-pro/v1.1. Arithmetic: 1024 x 1024 is
        # 1,048,576 pixels, which fal's own examples count as 1 MP (they start rounding
        # up at 1280x1024 = 1.31 MP -> 2 MP), so 1 MP x $0.04 = $0.04 = 4.0 cents. Read
        # strictly, 1.048 MP could instead round to 2 MP and cost 8 cents; this row takes
        # the lower reading, which can only undercharge us, never overcharge a person.
        # The two non-square presets this backend accepts (1024x1792 and 1792x1024) are
        # 1.84 MP and bill 2 MP, so a non-square image costs about twice this row.
        provider_cost_cents=4.0,
        infra_unit="none",  # infra via the credit floor (D-M3-4 amendment)
        cost_basis="estimate_static",
    ),
    PricingRow(
        surface="image",
        provider="cloudflare",
        sku="@cf/black-forest-labs/flux-1-schnell",
        unit="image",
        # "$0.0000528 per 512x512 tile" plus "$0.0001056 per step", from the Workers AI
        # pricing table, read 2026-09-19 at
        # https://developers.cloudflare.com/workers-ai/platform/pricing/. Arithmetic for
        # the request this backend sends (1024x1024 output, and steps=4 hard-coded in
        # CloudflareImageBackend._build_body): 4 tiles x $0.0000528 = $0.0002112, plus
        # 4 steps x $0.0001056 = $0.0004224, total $0.0006336 = 0.06336 cents. The same
        # page's neuron column agrees: 4 x 4.80 + 4 x 9.60 = 57.6 neurons at $0.011 per
        # 1000 neurons = $0.0006336, which is what confirms the two terms add rather than
        # multiply. Cloudflare's other two allowed models (SDXL base and dreamshaper) are
        # no longer on that price list, so they have no row and resolve to this one,
        # the cheapest image price we hold.
        provider_cost_cents=0.06336,
        infra_unit="none",  # infra via the credit floor (D-M3-4 amendment)
        cost_basis="estimate_static",
    ),
    # NVIDIA has NO row on purpose. The hosted catalog at integrate.api.nvidia.com is
    # credit-metered for prototyping and NVIDIA publishes no per-image list price for it;
    # production access is an AI Enterprise licence whose price is quote-only (checked
    # 2026-09-19 against build.nvidia.com and the Workers-AI-style pricing page NVIDIA
    # does not have). An nvidia image therefore still bills the floor and still warns,
    # which is the honest answer until the owner has an invoice to read a number off.
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


def served_row(surface: str, provider: str, model: str | None = None) -> PricingRow | None:
    """The registry row for a served ``(surface, provider)``, disambiguated by ``model``.

    A provider with several priced SKUs is resolved by ``model`` when it matches a row's
    ``sku``; otherwise the CHEAPEST priced row wins, so ambiguity can never over-charge. Rows
    carrying ``provider_cost_cents=None`` (the LLM resolver rows, priced from token metadata)
    are ignored: this answers "what does one unit of this surface cost", and those rows do not
    know. Lives here, with the rows, because voice and image both need the same question
    answered and a second copy of it is how two surfaces come to disagree about one price.
    """
    rows = [
        row
        for row in PRICING_ROWS
        if row.surface == surface
        and row.provider == provider
        and row.provider_cost_cents is not None
    ]
    if not rows:
        return None
    if model is not None:
        for row in rows:
            if row.sku == model:
                return row
    return min(rows, key=lambda row: row.provider_cost_cents or 0.0)
