"""Anthropic (Claude) static model metadata (Spec 23 T3; D-23-X-metadata-placement).

Pricing from anthropic.com/pricing (published, ``cost_verified_at_deploy=True``);
cost unit = cents per 1k tokens = ``$/Mtok × 0.1``. Quality normalised to
``[0.0, 1.0]`` from MMLU-Pro / GPQA-Diamond (R-23-2). Starter set — operators
extend per MAINTENANCE.md (D-23-3).
"""

from __future__ import annotations

from persona.backends.model_metadata import ModelMetadata

__all__ = ["MODELS"]

MODELS: dict[str, ModelMetadata] = {
    # $3 / $15 per Mtok (platform.claude.com/docs pricing, live-verified
    # 2026-07-12). 1M-token context at standard pricing (same page, long-context
    # section). Added at M2-T1 for coverage parity with the deleted runtime
    # ``_PRICE_TABLE`` — the deployed frontier fallback must keep estimating.
    "anthropic/claude-sonnet-4-6": ModelMetadata(
        cost_input_per_1k_tokens=0.30,
        cost_output_per_1k_tokens=1.50,
        latency_p50_ms=400.0,
        quality_benchmark=0.90,
        tools_supported=True,
        vision_supported=True,
        context_length=1_000_000,
    ),
    # $3 / $15 per Mtok STANDARD (platform.claude.com/docs pricing; released
    # 2026-06-30, standard rate from 2026-09-01 — the intro $2/$10 through
    # 2026-08-31 is deliberately NOT encoded, D-M3-R1). Same rate as sonnet-4-6.
    # The M3 authoring surface (D-M3-9) prices against this row when not
    # OpenRouter-served; an OpenRouter actual (usage.cost) supersedes. 1M context.
    "anthropic/claude-sonnet-5": ModelMetadata(
        cost_input_per_1k_tokens=0.30,
        cost_output_per_1k_tokens=1.50,
        latency_p50_ms=400.0,
        quality_benchmark=0.92,
        tools_supported=True,
        vision_supported=True,
        context_length=1_000_000,
    ),
    # $1 / $5 per Mtok (same page, live-verified 2026-07-12). NB the deleted
    # ``_PRICE_TABLE`` carried (0.08, 0.40) for this id — that is Haiku 3.5's
    # rate, a placeholder artifact; do not resurrect it. Added at M2-T1.
    "anthropic/claude-haiku-4-5": ModelMetadata(
        cost_input_per_1k_tokens=0.10,
        cost_output_per_1k_tokens=0.50,
        latency_p50_ms=250.0,
        quality_benchmark=0.80,
        tools_supported=True,
        vision_supported=True,
        context_length=200_000,
    ),
    # $3 / $15 per Mtok.
    "anthropic/claude-3.5-sonnet": ModelMetadata(
        cost_input_per_1k_tokens=0.30,
        cost_output_per_1k_tokens=1.50,
        latency_p50_ms=400.0,
        quality_benchmark=0.88,
        tools_supported=True,
        vision_supported=True,
        context_length=200_000,
    ),
    # $0.80 / $4 per Mtok.
    "anthropic/claude-3.5-haiku": ModelMetadata(
        cost_input_per_1k_tokens=0.08,
        cost_output_per_1k_tokens=0.40,
        latency_p50_ms=250.0,
        quality_benchmark=0.72,
        tools_supported=True,
        vision_supported=True,
        context_length=200_000,
    ),
}
