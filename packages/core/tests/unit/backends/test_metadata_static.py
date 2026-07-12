"""Unit tests for Spec 23 T3 static metadata tables + StaticModelMetadataResolver."""

from __future__ import annotations

import pytest
from persona.backends.metadata import (
    STATIC_MODEL_METADATA,
    StaticModelMetadataResolver,
    anthropic,
    deepseek,
    google,
    groq,
    nvidia,
    openai,
)
from persona.backends.metadata.nvidia import MODELS as NVIDIA_MODELS
from persona.backends.model_metadata import ModelMetadata, ModelMetadataResolver

ALL_TABLES = (
    anthropic.MODELS,
    openai.MODELS,
    google.MODELS,
    deepseek.MODELS,
    groq.MODELS,
    nvidia.MODELS,
)


class TestProviderTables:
    @pytest.mark.parametrize("table", ALL_TABLES)
    def test_every_entry_is_model_metadata(self, table: dict[str, ModelMetadata]) -> None:
        assert table, "provider table should not be empty"
        for model_id, md in table.items():
            assert isinstance(md, ModelMetadata)
            assert "/" in model_id, "ids must be provider-prefixed canonical form"

    def test_keys_are_disjoint_across_providers(self) -> None:
        seen: set[str] = set()
        for table in ALL_TABLES:
            overlap = seen & set(table)
            assert not overlap, f"duplicate ids across provider tables: {overlap}"
            seen |= set(table)

    def test_merged_table_is_union(self) -> None:
        total = sum(len(t) for t in ALL_TABLES)
        assert len(STATIC_MODEL_METADATA) == total

    def test_nvidia_rows_flag_verify_at_deploy(self) -> None:
        # R-23-2: NVIDIA has no public $/Mtok → cost is best-estimate.
        for md in NVIDIA_MODELS.values():
            assert md.cost_verified_at_deploy is False


class TestStaticResolver:
    def test_resolver_satisfies_protocol(self) -> None:
        assert isinstance(StaticModelMetadataResolver(), ModelMetadataResolver)

    def test_resolves_known_model(self) -> None:
        resolver = StaticModelMetadataResolver()
        md = resolver.resolve("anthropic/claude-3.5-sonnet")
        assert md is not None
        assert md.tools_supported is True
        assert md.vision_supported is True

    def test_returns_none_on_miss(self) -> None:
        assert StaticModelMetadataResolver().resolve("acme/does-not-exist") is None

    def test_accepts_injected_table(self) -> None:
        custom = {
            "x/y": ModelMetadata(
                cost_input_per_1k_tokens=0.1,
                cost_output_per_1k_tokens=0.2,
                latency_p50_ms=100.0,
                quality_benchmark=0.5,
                tools_supported=False,
                vision_supported=False,
                context_length=8_000,
            )
        }
        resolver = StaticModelMetadataResolver(table=custom)
        assert resolver.resolve("x/y") is not None
        assert resolver.resolve("anthropic/claude-3.5-sonnet") is None


class TestM2ParityRows:
    """Spec M2 (M2-T1, F4): the deleted runtime ``_PRICE_TABLE``'s coverage.

    The three keys the static tables did NOT already carry (current-gen
    anthropic + the groq module) were added with vendor-page-verified numbers
    (live-verified 2026-07-12) so deleting the placeholder table regresses
    nothing to unpriced.
    """

    def test_claude_sonnet_4_6_row(self) -> None:
        md = STATIC_MODEL_METADATA["anthropic/claude-sonnet-4-6"]
        # $3 / $15 per Mtok → cents/1k = $/Mtok × 0.1.
        assert md.cost_input_per_1k_tokens == pytest.approx(0.30)
        assert md.cost_output_per_1k_tokens == pytest.approx(1.50)
        assert md.cost_verified_at_deploy is True
        assert md.context_length == 1_000_000  # 1M standard (pricing docs)

    def test_claude_haiku_4_5_row(self) -> None:
        md = STATIC_MODEL_METADATA["anthropic/claude-haiku-4-5"]
        # $1 / $5 per Mtok — NOT the deleted table's Haiku-3.5 placeholder
        # (0.08 / 0.40).
        assert md.cost_input_per_1k_tokens == pytest.approx(0.10)
        assert md.cost_output_per_1k_tokens == pytest.approx(0.50)
        assert md.cost_verified_at_deploy is True

    def test_groq_llama_row(self) -> None:
        md = STATIC_MODEL_METADATA["groq/llama-3.1-8b-instant"]
        # $0.05 / $0.08 per Mtok (groq.com/pricing).
        assert md.cost_input_per_1k_tokens == pytest.approx(0.005)
        assert md.cost_output_per_1k_tokens == pytest.approx(0.008)
        assert md.cost_verified_at_deploy is True
        assert md.vision_supported is False


class TestNvidiaReconciliation:
    """D-23-X-metadata-placement: tier table derives from the core numbers home."""

    def test_tier_table_derives_from_core_numbers(self) -> None:
        from persona_runtime.routing.nvidia_models import NVIDIA_LAUNCH_MODEL_METADATA

        for model_id, core_md in NVIDIA_MODELS.items():
            tier_md = NVIDIA_LAUNCH_MODEL_METADATA[model_id]
            # The shared numbers come from ONE place (the core table).
            assert tier_md.cost_input_per_1k_tokens == core_md.cost_input_per_1k_tokens
            assert tier_md.cost_output_per_1k_tokens == core_md.cost_output_per_1k_tokens
            assert tier_md.first_token_latency_ms == core_md.latency_p50_ms
            assert tier_md.context_window == core_md.context_length
            assert tier_md.cost_verified_at_deploy == core_md.cost_verified_at_deploy
