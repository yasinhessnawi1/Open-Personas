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
    # R9-115: the three entries below were the ENTIRE Groq table, and all three are
    # gone from the live catalogue (verified against GET /v1/models, 2026-09-02: the
    # account lists 14 models and none of these is among them). They are kept only as
    # a record; nothing should route to them. The live replacements follow.
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
    # $0.59 / $0.79 per Mtok (groq.com/pricing, live-verified 2026-07-18). The
    # PERSONA_MID_MODELS / PERSONA_SMALL_MODELS primary — voice's served model
    # (M3 F2: was unpriced, so voice LLM turns recorded ``unpriced``). No vision.
    "groq/llama-3.3-70b-versatile": ModelMetadata(
        cost_input_per_1k_tokens=0.059,
        cost_output_per_1k_tokens=0.079,
        latency_p50_ms=250.0,
        quality_benchmark=0.68,
        tools_supported=True,
        vision_supported=False,
        context_length=128_000,
    ),
    # $0.11 / $0.34 per Mtok (groq.com/pricing, live-verified 2026-07-18). A
    # PERSONA_MID_MODELS fallback; Llama 4 Scout is multimodal (vision).
    "groq/meta-llama/llama-4-scout-17b-16e-instruct": ModelMetadata(
        cost_input_per_1k_tokens=0.011,
        cost_output_per_1k_tokens=0.034,
        latency_p50_ms=200.0,
        quality_benchmark=0.56,
        tools_supported=True,
        vision_supported=True,
        context_length=128_000,
    ),
    # --- live models, verified against the Groq catalogue 2026-09-02 -------------
    # $0.04 / $0.17 per Mtok (published rates, cross-checked via the OpenRouter
    # catalogue for the same underlying model). Native tool calling CONFIRMED with a
    # real tool-call round trip, which matters because the mid tier dispatches tools.
    #
    # REASONING MODEL, and this is load-bearing: it spends completion tokens on a
    # `reasoning` field BEFORE emitting content. A probe with max_tokens=8 returned
    # finish_reason=length and EMPTY content; at 200 it answered normally. So a caller
    # with a tight output budget gets an empty completion, which is exactly the
    # R9-033 EmptyCompletionError shape. Do not pair this model with a small
    # max_tokens.
    "groq/openai/gpt-oss-120b": ModelMetadata(
        cost_input_per_1k_tokens=0.004,
        cost_output_per_1k_tokens=0.017,
        latency_p50_ms=250.0,
        quality_benchmark=0.70,
        tools_supported=True,
        vision_supported=False,
        context_length=128_000,
    ),
    # $0.03 / $0.13 per Mtok, same sourcing. The smaller sibling; same reasoning-token
    # caveat as the 120b above.
    "groq/openai/gpt-oss-20b": ModelMetadata(
        cost_input_per_1k_tokens=0.003,
        cost_output_per_1k_tokens=0.013,
        latency_p50_ms=200.0,
        quality_benchmark=0.55,
        tools_supported=True,
        vision_supported=False,
        context_length=128_000,
    ),
}
