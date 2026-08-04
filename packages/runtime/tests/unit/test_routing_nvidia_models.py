"""Unit tests for Spec 20 T14 — NVIDIA launch-set TierMetadata registry.

Covers the static :data:`NVIDIA_LAUNCH_MODEL_METADATA` table + the
:func:`nvidia_metadata_for_model` lookup helper, then exercises the
:func:`~persona_runtime.routing.scoring.score_tier` integration for the
D-18-5 quality-proxy boost on reasoning-capable tiers.
"""

from __future__ import annotations

import pytest
from persona.backends import BackendConfig
from persona_runtime.routing import (
    NVIDIA_LAUNCH_MODEL_METADATA,
    RoutingContext,
    nvidia_metadata_for_model,
)
from persona_runtime.routing.scoring import score_tier
from persona_runtime.tier import TierConfig, TierMetadata, TierRegistry
from pydantic import ValidationError

# ----- Static registry shape --------------------------------------------------


class TestNvidiaLaunchModelMetadata:
    """The D-20-1 launch set must be present + each entry well-formed."""

    def test_registry_covers_every_priced_nvidia_model(self) -> None:
        # Was `len(...) == 3` for the D-20-1 launch set (49b-v1.5 chat +
        # 120b-a12b long-context/reasoning + nano-omni-30b reasoning+vision;
        # imagegen FLUX.2-klein-4b is NOT in this chat-side registry, it lives
        # under Spec 20 T16's image-backend). R9-105 added the 550B Ultra as the
        # frontier model, so a bare count now just says "somebody added a row".
        #
        # Pins the INVARIANT instead: this registry and the core price table
        # describe the same set of models. A model in one but not the other is
        # the actual defect -- routing metadata without a price, or a priced
        # model the router cannot rank.
        from persona.backends.metadata.nvidia import MODELS as CORE_MODELS

        assert set(NVIDIA_LAUNCH_MODEL_METADATA) == set(CORE_MODELS), (
            "the routing registry and the core price table have diverged"
        )

    def test_chat_primary_present(self) -> None:
        assert "nvidia/llama-3.3-nemotron-super-49b-v1.5" in NVIDIA_LAUNCH_MODEL_METADATA

    def test_long_context_reasoning_present(self) -> None:
        assert "nvidia/nemotron-3-super-120b-a12b" in NVIDIA_LAUNCH_MODEL_METADATA

    def test_nano_omni_reasoning_present(self) -> None:
        assert "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning" in NVIDIA_LAUNCH_MODEL_METADATA

    def test_all_entries_are_tier_metadata(self) -> None:
        for entry in NVIDIA_LAUNCH_MODEL_METADATA.values():
            assert isinstance(entry, TierMetadata)


# ----- Reasoning-capable flag per D-18-5 + D-20-1 ----------------------------


class TestNvidiaReasoningCapableFlag:
    """D-18-5 quality-proxy boost depends on this flag — verify per D-20-1."""

    def test_chat_primary_is_not_reasoning_capable(self) -> None:
        # 49b-v1.5 is the chat primary, NOT a reasoning-tuned variant.
        md = NVIDIA_LAUNCH_MODEL_METADATA["nvidia/llama-3.3-nemotron-super-49b-v1.5"]
        assert md.reasoning_capable is False

    def test_120b_a12b_is_reasoning_capable(self) -> None:
        # 120b-a12b serves the reasoning tier via enable_thinking extra_body.
        md = NVIDIA_LAUNCH_MODEL_METADATA["nvidia/nemotron-3-super-120b-a12b"]
        assert md.reasoning_capable is True

    def test_nano_omni_is_reasoning_capable(self) -> None:
        # Nano-Omni reasoning variant: omni-modal + dedicated reasoning.
        md = NVIDIA_LAUNCH_MODEL_METADATA["nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"]
        assert md.reasoning_capable is True


# ----- D-13-3 verify-at-deploy convention ------------------------------------


class TestNvidiaVerifyAtDeploy:
    """Every NVIDIA launch entry MUST flag costs as verify-at-deploy (R-20-4)."""

    @pytest.mark.parametrize("model_id", list(NVIDIA_LAUNCH_MODEL_METADATA))
    def test_cost_verified_at_deploy_is_false(self, model_id: str) -> None:
        md = NVIDIA_LAUNCH_MODEL_METADATA[model_id]
        assert md.cost_verified_at_deploy is False

    @pytest.mark.parametrize("model_id", list(NVIDIA_LAUNCH_MODEL_METADATA))
    def test_cost_fields_are_non_negative_estimates(self, model_id: str) -> None:
        # Best-estimate values per R-20-1 / R-20-7 mid-range — non-negative
        # so the Layer 2 scorer's cost formula doesn't produce a poisoned
        # negative value before the operator measures-and-overrides.
        md = NVIDIA_LAUNCH_MODEL_METADATA[model_id]
        assert md.cost_input_per_1k_tokens >= 0.0
        assert md.cost_output_per_1k_tokens >= 0.0


# ----- Tool strength + context windows per R-20-1 ----------------------------


class TestNvidiaToolStrength:
    @pytest.mark.parametrize("model_id", list(NVIDIA_LAUNCH_MODEL_METADATA))
    def test_native_tool_calling_strong(self, model_id: str) -> None:
        # R-20-1 confirms native tool calling across the launch set.
        md = NVIDIA_LAUNCH_MODEL_METADATA[model_id]
        assert md.tool_strength == "strong"


class TestNvidiaContextWindows:
    def test_49b_v1_5_context_is_128k(self) -> None:
        md = NVIDIA_LAUNCH_MODEL_METADATA["nvidia/llama-3.3-nemotron-super-49b-v1.5"]
        assert md.context_window == 131072

    def test_120b_a12b_context_is_1m(self) -> None:
        md = NVIDIA_LAUNCH_MODEL_METADATA["nvidia/nemotron-3-super-120b-a12b"]
        assert md.context_window == 1_000_000


# ----- Lookup helper ----------------------------------------------------------


class TestNvidiaMetadataForModel:
    def test_returns_metadata_for_known_model(self) -> None:
        md = nvidia_metadata_for_model("nvidia/nemotron-3-super-120b-a12b")
        assert md is not None
        assert md.reasoning_capable is True

    def test_returns_none_for_unknown_model(self) -> None:
        assert nvidia_metadata_for_model("nvidia/some-unlisted-model") is None

    def test_returns_none_for_non_nvidia_model(self) -> None:
        assert nvidia_metadata_for_model("anthropic/claude-sonnet-4-6") is None


# ----- D-18-5 quality-proxy boost integration --------------------------------


def _ctx(**overrides: object) -> RoutingContext:
    defaults: dict[str, object] = {
        "requires_vision": False,
        "estimated_input_tokens": 0,
        "requires_strong_tools": False,
        "is_first_turn": False,
        "is_identity_sensitive": False,
        "is_boilerplate": False,
        "conversation_phase": "middle",
        "profile": "text_default",
    }
    defaults.update(overrides)
    return RoutingContext(**defaults)  # type: ignore[arg-type]


def _registry(*tiers: tuple[str, TierMetadata | None]) -> TierRegistry:
    return TierRegistry(
        {
            name: TierConfig(
                name=name,
                backend_config=BackendConfig(provider="nvidia", model=name, api_key="nvapi-test"),
                metadata=md,
            )
            for name, md in tiers
        }
    )


class TestReasoningCapableBoostOnHardTurns:
    """D-18-5 + Spec 20 T14 — reasoning-capable up-weight on high quality_proxy."""

    def test_boost_applied_on_hard_turn(self) -> None:
        # Apples-to-apples: same tier NAME (frontier) gets scored twice — once
        # with reasoning_capable=True, once False. Holding the tier-quality
        # estimate constant (frontier=1.0) isolates the boost as the only
        # variable. Hard turn (first + identity-sensitive → quality_proxy=0.60
        # ≥ 0.5).
        non_reasoning = TierMetadata(
            cost_input_per_1k_tokens=1.0,
            cost_output_per_1k_tokens=5.0,
            first_token_latency_ms=600.0,
            throughput_tokens_per_sec=30.0,
            context_window=200_000,
            tool_strength="strong",
            reasoning_capable=False,
        )
        reasoning = non_reasoning.model_copy(update={"reasoning_capable": True})
        registry_reasoning = _registry(("frontier", reasoning))
        registry_baseline = _registry(("frontier", non_reasoning))
        hard = _ctx(is_first_turn=True, is_identity_sensitive=True)
        s_reasoning = score_tier("frontier", hard, registry_reasoning)
        s_baseline = score_tier("frontier", hard, registry_baseline)
        assert s_reasoning is not None
        assert s_baseline is not None
        # Reasoning tier scores strictly higher than the non-reasoning
        # baseline on the same axes (quality_fit floor + reasoning boost).
        assert s_reasoning > s_baseline

    def test_no_boost_on_easy_turn(self) -> None:
        # Easy turn (boilerplate, no signals) → quality_proxy=0.0 < 0.5.
        # Reasoning-capable flag must NOT up-weight — preserves cost-sensitive
        # routing for routine traffic.
        reasoning = TierMetadata(
            cost_input_per_1k_tokens=1.0,
            cost_output_per_1k_tokens=5.0,
            first_token_latency_ms=600.0,
            throughput_tokens_per_sec=30.0,
            context_window=200_000,
            tool_strength="strong",
            reasoning_capable=True,
        )
        non_reasoning = reasoning.model_copy(update={"reasoning_capable": False})
        registry = _registry(("mid", reasoning))
        registry_baseline = _registry(("mid", non_reasoning))
        easy = _ctx(profile="text_default", is_boilerplate=True)
        s_reasoning = score_tier("mid", easy, registry)
        s_baseline = score_tier("mid", easy, registry_baseline)
        assert s_reasoning is not None
        assert s_baseline is not None
        # Equal scores: below the threshold the reasoning flag is neutral.
        assert s_reasoning == pytest.approx(s_baseline)

    def test_verify_at_deploy_cost_estimates_do_not_poison_scorer(self) -> None:
        # NVIDIA launch entries with cost_verified_at_deploy=False have
        # best-estimate cost values — the scorer must still produce a finite
        # float in the plausible range (not NaN / not negative).
        md = NVIDIA_LAUNCH_MODEL_METADATA["nvidia/nemotron-3-super-120b-a12b"]
        registry = _registry(("frontier", md))
        result = score_tier("frontier", _ctx(is_first_turn=True), registry)
        assert result is not None
        # Bounded by [0.0, 2.0] (cost+quality_fit+latency can each contribute
        # up to ~1.0 plus 0.10 reasoning boost — well under 2.0).
        assert 0.0 <= result <= 2.0


# ----- Frozen / extra=forbid invariants on the new TierMetadata fields -------


class TestNvidiaRegistryEntriesAreFrozen:
    @pytest.mark.parametrize("model_id", list(NVIDIA_LAUNCH_MODEL_METADATA))
    def test_entry_is_frozen(self, model_id: str) -> None:
        md = NVIDIA_LAUNCH_MODEL_METADATA[model_id]
        with pytest.raises(ValidationError):
            md.cost_input_per_1k_tokens = 99.0  # type: ignore[misc]
