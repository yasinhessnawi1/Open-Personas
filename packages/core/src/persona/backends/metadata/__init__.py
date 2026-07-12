"""Static per-provider model-metadata tables — the one authoritative numbers home.

Spec 23 D-23-X-metadata-placement: every per-model number (pricing, latency,
benchmark, capability, context) has **exactly one hand-kept home** here in
``persona.backends.metadata``. The Spec 18 tier layer
(:data:`persona_runtime.routing.nvidia_models.NVIDIA_LAUNCH_MODEL_METADATA`)
*derives* its :class:`TierMetadata` from these rows rather than keeping a second
copy — the acceptance test is "one file is edited when a provider changes a
price".

Cost unit is **cents per 1k tokens** (identical to
:class:`~persona_runtime.tier.TierMetadata`), i.e. ``$/Mtok × 0.1``. Quality is
normalised to ``[0.0, 1.0]`` at author time (D-23-4 — MMLU-Pro / GPQA-Diamond,
NOT raw MMLU). Rows whose provider publishes no authoritative ``$/Mtok`` (NVIDIA,
R-23-2) carry ``cost_verified_at_deploy=False`` so the scorer skips / down-weights
their cost axis.

These tables are a **maintained starter set**, not exhaustive — operators add
their deployment's exact model ids and refresh on the
``MAINTENANCE.md`` cadence (D-23-3: quarterly + on-provider-price-change). A
model absent from both these tables AND the OpenRouter catalog is a metadata
miss → the IntelligentRouter degrades to rule-based selection (D-23-5, criterion
9), never crashing.
"""

from __future__ import annotations

from persona.backends.metadata import anthropic, deepseek, google, groq, nvidia, openai
from persona.backends.metadata.chained_resolver import ChainedModelMetadataResolver
from persona.backends.metadata.openrouter_resolver import OpenRouterModelMetadataResolver
from persona.backends.metadata.static_resolver import (
    STATIC_MODEL_METADATA,
    StaticModelMetadataResolver,
)

__all__ = [
    "STATIC_MODEL_METADATA",
    "ChainedModelMetadataResolver",
    "OpenRouterModelMetadataResolver",
    "StaticModelMetadataResolver",
    "anthropic",
    "deepseek",
    "google",
    "groq",
    "nvidia",
    "openai",
]
