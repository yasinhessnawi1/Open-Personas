"""NVIDIA (Nemotron / NIM) static model metadata — the authoritative numbers home.

Spec 23 D-23-X-metadata-placement: these rows are the **single hand-kept copy**
of the NVIDIA launch-set per-model numbers. The Spec 20 tier table
(:data:`persona_runtime.routing.nvidia_models.NVIDIA_LAUNCH_MODEL_METADATA`)
DERIVES its :class:`TierMetadata` from these rows + tier-only fields
(throughput / tool_strength / reasoning_capable) — no second copy of cost,
latency, context, or the verify-at-deploy flag.

``cost_verified_at_deploy=False`` on every row: NVIDIA publishes no authoritative
``$/Mtok`` on the hosted catalog (R-20-4 / R-23-2 — free tier is rate-limit
gated, production is AI-Enterprise + own GPU). The cost fields are best-estimate
mid-range; the scorer skips / down-weights the cost axis for these candidates and
operators must measure-and-override at deploy. Quality is a normalised
best-estimate (Nemotron technical reports, R-23-2). Cost unit = cents per 1k
tokens.
"""

from __future__ import annotations

from persona.backends.model_metadata import ModelMetadata

__all__ = ["MODELS"]

MODELS: dict[str, ModelMetadata] = {
    # Chat primary — 128k context, native tools, NOT a dedicated reasoning model.
    # R9-104 UNVERIFIED: every entry in this file carries `cost_verified_at_deploy=False`,
    # and it is the ONLY provider table where that is true — anthropic / deepseek / google /
    # groq / openai are all verified. One of these prices (nemotron-3-super-120b) proved to be
    # ~50-94x the published rate and drained a paid account in hours before it was caught.
    # The two remaining entries below are still unverified and are very likely high on the
    # same pattern ($3/M and $1.50/M input against a NVIDIA catalogue whose blended average is
    # ~$0.85/M). They are NOT corrected here because no per-model source was confirmed for
    # them, and guessing a price is how this defect was introduced. Verify against the live
    # catalogue before either is put on a serving chain.
    "nvidia/llama-3.3-nemotron-super-49b-v1.5": ModelMetadata(
        cost_input_per_1k_tokens=0.30,
        cost_output_per_1k_tokens=0.60,
        latency_p50_ms=300.0,
        quality_benchmark=0.70,
        tools_supported=True,
        vision_supported=False,
        context_length=131_072,
        cost_verified_at_deploy=False,
    ),
    # Long-context + reasoning (enable_thinking) — 1M context, flagship.
    # R9-104: was 1.50 / 7.50 — i.e. $15/M input and $75/M output, Claude-Opus-class
    # pricing for a mid-tier Nemotron. Published rates are a median $0.30/M input and
    # $0.80/M output (as low as $0.10/$0.50 on some hosts), so the table overcharged by
    # ~50× on input and ~94× on output. This is the model the PAID frontier chain
    # actually serves, so every background leg was billed against it: the owner's 6000
    # credits were consumed in ~9 hours at ~240/leg when the real cost was nearer 3-5.
    # `cost_verified_at_deploy=False` was flagging this the whole time.
    "nvidia/nemotron-3-super-120b-a12b": ModelMetadata(
        cost_input_per_1k_tokens=0.03,
        cost_output_per_1k_tokens=0.08,
        latency_p50_ms=600.0,
        quality_benchmark=0.82,
        tools_supported=True,
        vision_supported=False,
        context_length=1_000_000,
        cost_verified_at_deploy=False,
    ),
    # Reasoning + vision (omni-modal). Cheaper reasoning option; 30B/3B MoE.
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": ModelMetadata(
        cost_input_per_1k_tokens=0.15,
        cost_output_per_1k_tokens=0.30,
        latency_p50_ms=300.0,
        quality_benchmark=0.66,
        tools_supported=True,
        vision_supported=True,
        context_length=32_768,
        cost_verified_at_deploy=False,
    ),
}
