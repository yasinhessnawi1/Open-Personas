"""Groq static model metadata (Spec M2 T1; D-23-X-metadata-placement).

Pricing from groq.com/pricing (published, ``cost_verified_at_deploy=True``;
live-verified 2026-07-12); cost unit = cents per 1k tokens = ``$/Mtok × 0.1``.
Quality normalised to ``[0.0, 1.0]`` from MMLU-Pro / GPQA (R-23-2). Added at
M2-T1 for coverage parity with the deleted runtime ``_PRICE_TABLE`` (its
``("groq", "llama-3.1-8b-instant")`` key must keep resolving through the
Spec-23 chain). Starter set — operators extend per MAINTENANCE.md (D-23-3).
"""

from __future__ import annotations

from persona.backends.model_metadata import ModelMetadata

__all__ = ["MODELS"]

MODELS: dict[str, ModelMetadata] = {
    # $0.05 / $0.08 per Mtok (groq.com/pricing "128k" context, 2026-07-12).
    # No vision. LPU serving — very low first-token latency (~840 tok/s
    # published throughput).
    "groq/llama-3.1-8b-instant": ModelMetadata(
        cost_input_per_1k_tokens=0.005,
        cost_output_per_1k_tokens=0.008,
        latency_p50_ms=200.0,
        quality_benchmark=0.45,
        tools_supported=True,
        vision_supported=False,
        context_length=128_000,
    ),
}
