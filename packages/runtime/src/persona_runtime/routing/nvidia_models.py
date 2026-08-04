"""Spec 20 T14 — NVIDIA launch-set TierMetadata registry (D-20-1; R-20-1, R-20-4, R-20-7).

Static lookup table mapping the D-20-1 launch NVIDIA model IDs to recommended
:class:`~persona_runtime.tier.TierMetadata` defaults. Used by operators (and
tests) as a starting point for per-tier metadata — the actual deployment can
still override every field via the ``PERSONA_<TIER>_*`` env-var schema read by
:func:`~persona_runtime.tier.tier_metadata_from_env`.

**verify-at-deploy convention** (D-13-3 precedent; R-20-4 finding): NVIDIA does
not publish authoritative ``$/Mtok`` on the hosted catalog — the free tier is
rate-limit gated and production deployments need AI Enterprise. The cost
fields here therefore carry **best-estimate mid-range values** and the entries
set :attr:`TierMetadata.cost_verified_at_deploy` to ``False`` to flag
operators they must measure-and-override before relying on Layer 2 cost
weighting.

**reasoning-capable flag** (D-18-5 quality-proxy boost; D-20-1): Nemotron-3
Super 120b (via ``enable_thinking``) and Nano-Omni 30b reasoning variant are
flagged ``reasoning_capable=True``; the chat-primary 49b-v1.5 stays ``False``.
The scorer (:func:`persona_runtime.routing.scoring.score_tier`) up-weights
``reasoning_capable=True`` tiers on hard turns (``quality_proxy >= 0.5``).

**Latency / throughput estimates** sourced from R-20-1 (model card audit) and
R-20-7 (image-gen profile measurements extrapolated for inference); ranges
chosen at midpoint of confidence band. Operators should re-measure against
their NIM deployment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from persona.backends.metadata import nvidia as _nvidia_metadata

from persona_runtime.tier import TierMetadata

__all__ = [
    "NVIDIA_LAUNCH_MODEL_METADATA",
    "nvidia_metadata_for_model",
]


# --- Launch set per D-20-1 ----------------------------------------------------
#
# Spec 23 D-23-X-metadata-placement: the per-model NUMBERS (cost / latency /
# context / cost_verified) live in ONE authoritative home —
# ``persona.backends.metadata.nvidia.MODELS``. This tier table DERIVES its
# :class:`TierMetadata` from those rows plus the tier-only fields below
# (throughput / tool_strength / reasoning_capable, which the model-layer
# :class:`~persona.backends.model_metadata.ModelMetadata` does not carry) — there
# is NO second hand-kept copy of any number. Three chat / reasoning surfaces; the
# imagegen launch model (FLUX.2-klein-4b) lives outside the chat TierRegistry
# path (Spec 20 T16's MultiModelImageBackend).


@dataclass(frozen=True)
class _TierOnly:
    """Tier-layer fields not present on the model-layer ``ModelMetadata``.

    ``throughput_tokens_per_sec`` and ``reasoning_capable`` are Spec 18 / D-18-5
    tier-scoring concerns; ``tool_strength`` is the Layer 1 categorical the
    model-layer expresses only as the boolean ``tools_supported``. These are the
    only NVIDIA numbers kept here — everything else derives from the core table.
    """

    throughput_tokens_per_sec: float
    tool_strength: Literal["weak", "medium", "strong"]
    reasoning_capable: bool


# R-20-1 throughput midpoints; reasoning_capable per D-20-1 (49b chat-primary is
# NOT reasoning; 120b enable_thinking + nano-omni reasoning variant ARE).
_TIER_ONLY_FIELDS: dict[str, _TierOnly] = {
    "nvidia/llama-3.3-nemotron-super-49b-v1.5": _TierOnly(45.0, "strong", False),
    "nvidia/nemotron-3-super-120b-a12b": _TierOnly(30.0, "strong", True),
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning": _TierOnly(55.0, "strong", True),
    # R9-105: the 550B Ultra frontier model. Hybrid Transformer-Mamba MoE with 55B
    # active of 550B, so throughput is closer to a mid-size dense model than the
    # parameter count suggests; reasoning_capable per its published extended-reasoning
    # support. The cost / latency / context numbers are NOT repeated here — they derive
    # from the core table, which is the point of this split (D-23-X-metadata-placement).
    "nvidia/nemotron-3-ultra-550b-a55b": _TierOnly(25.0, "strong", True),
}


def _tier_metadata_for(model_id: str) -> TierMetadata:
    """Compose a :class:`TierMetadata` from the core model row + tier-only fields."""
    model = _nvidia_metadata.MODELS[model_id]
    extra = _TIER_ONLY_FIELDS[model_id]
    return TierMetadata(
        cost_input_per_1k_tokens=model.cost_input_per_1k_tokens,
        cost_output_per_1k_tokens=model.cost_output_per_1k_tokens,
        first_token_latency_ms=model.latency_p50_ms,
        throughput_tokens_per_sec=extra.throughput_tokens_per_sec,
        context_window=model.context_length,
        tool_strength=extra.tool_strength,
        reasoning_capable=extra.reasoning_capable,
        cost_verified_at_deploy=model.cost_verified_at_deploy,
    )


NVIDIA_LAUNCH_MODEL_METADATA: dict[str, TierMetadata] = {
    model_id: _tier_metadata_for(model_id) for model_id in _TIER_ONLY_FIELDS
}


def nvidia_metadata_for_model(model_id: str) -> TierMetadata | None:
    """Return the recommended :class:`TierMetadata` for an NVIDIA launch model.

    Args:
        model_id: Provider-prefixed model ID, e.g.
            ``"nvidia/llama-3.3-nemotron-super-49b-v1.5"``.

    Returns:
        The launch-set :class:`TierMetadata` recommendation when ``model_id``
        is a D-20-1 launch entry; ``None`` for any other model (operators are
        expected to populate metadata via the
        :func:`~persona_runtime.tier.tier_metadata_from_env` env-var path
        for non-launch models).
    """
    return NVIDIA_LAUNCH_MODEL_METADATA.get(model_id)
