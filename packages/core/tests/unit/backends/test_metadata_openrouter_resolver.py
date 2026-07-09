"""Unit tests for Spec 23 T4 OpenRouterModelMetadataResolver."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from loguru import logger as _loguru_logger
from persona.backends.metadata.openrouter_resolver import OpenRouterModelMetadataResolver
from persona.backends.model_metadata import ModelMetadataResolver
from persona.backends.openrouter_catalog import OpenRouterCatalogClient

if TYPE_CHECKING:
    from collections.abc import Iterator

_CATALOG = {
    "data": [
        {
            "id": "anthropic/claude-3.5-sonnet",
            "canonical_slug": "anthropic/claude-3.5-sonnet",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools", "temperature"],
        },
        {
            "id": "deepseek/deepseek-chat",
            "context_length": 128000,
            "pricing": {"prompt": "0.00000027", "completion": "0.0000011"},
            "architecture": {"input_modalities": ["text"]},
            "supported_parameters": ["tools"],
        },
    ]
}


def _client(
    payload: dict[str, object] | None = None, *, status: int = 200
) -> OpenRouterCatalogClient:
    body = payload if payload is not None else _CATALOG

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={})

    return OpenRouterCatalogClient("sk-or-test", transport=httpx.MockTransport(handler))


class TestOpenRouterResolver:
    def test_satisfies_protocol(self) -> None:
        assert isinstance(OpenRouterModelMetadataResolver(_client()), ModelMetadataResolver)

    def test_maps_pricing_to_cents_per_1k(self) -> None:
        # $3/Mtok = "0.000003" USD/token → 0.30 cents/1k; $15/Mtok → 1.50.
        md = OpenRouterModelMetadataResolver(_client()).resolve("anthropic/claude-3.5-sonnet")
        assert md is not None
        assert md.cost_input_per_1k_tokens == pytest.approx(0.30)
        assert md.cost_output_per_1k_tokens == pytest.approx(1.50)
        assert md.context_length == 200000

    def test_maps_capability_flags(self) -> None:
        resolver = OpenRouterModelMetadataResolver(_client())
        sonnet = resolver.resolve("anthropic/claude-3.5-sonnet")
        chat = resolver.resolve("deepseek/deepseek-chat")
        assert sonnet is not None
        assert chat is not None
        assert sonnet.tools_supported is True
        assert sonnet.vision_supported is True  # "image" in input_modalities
        assert chat.vision_supported is False

    def test_strips_dynamic_variant_before_lookup(self) -> None:
        md = OpenRouterModelMetadataResolver(_client()).resolve("anthropic/claude-3.5-sonnet:nitro")
        assert md is not None

    def test_returns_none_on_miss(self) -> None:
        assert OpenRouterModelMetadataResolver(_client()).resolve("acme/unknown") is None

    def test_fail_open_on_catalog_error(self) -> None:
        # 500 → OpenRouterCatalogError inside list_models → resolver degrades to
        # empty index, returns None (defer to static fallback), never raises.
        resolver = OpenRouterModelMetadataResolver(_client(status=500))
        assert resolver.resolve("anthropic/claude-3.5-sonnet") is None

    def test_openrouter_only_model_gets_neutral_quality_and_latency(self) -> None:
        md = OpenRouterModelMetadataResolver(_client()).resolve("deepseek/deepseek-chat")
        assert md is not None
        assert md.quality_benchmark == 0.5  # neutral sentinel (no catalog benchmark)
        assert md.cost_verified_at_deploy is False  # derived pricing

    def test_negative_sentinel_pricing_entry_is_skipped_not_crash(self) -> None:
        # Operator-pass regression: the LIVE catalog carries entries with
        # negative sentinel pricing ("-1" = variable / not-applicable) which
        # violate ModelMetadata's cost ge=0 bound. The resolver must skip such
        # entries (metadata miss → static fallback), NOT raise ValidationError.
        catalog = {
            "data": [
                {
                    "id": "weird/variable-priced",
                    "context_length": 8000,
                    "pricing": {"prompt": "-1", "completion": "-1"},
                    "architecture": {"input_modalities": ["text"]},
                    "supported_parameters": ["tools"],
                },
                {
                    "id": "good/model",
                    "context_length": 128000,
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                    "architecture": {"input_modalities": ["text"]},
                    "supported_parameters": ["tools"],
                },
            ]
        }
        resolver = OpenRouterModelMetadataResolver(_client(catalog))
        # The bad entry is skipped (miss), the good entry still resolves.
        assert resolver.resolve("weird/variable-priced") is None
        assert resolver.resolve("good/model") is not None

    def test_multiple_invalid_entries_emit_one_summary_not_per_entry(
        self, log_records: list[dict[str, Any]]
    ) -> None:
        # R9-018: several sentinel-priced entries must produce ONE summary
        # WARNING (with the count), NOT one WARNING per entry (log spam).
        # Per-entry detail stays at DEBUG.
        catalog = {
            "data": [
                {
                    "id": f"weird/variable-{i}",
                    "context_length": 8000,
                    "pricing": {"prompt": "-1", "completion": "-1"},
                    "architecture": {"input_modalities": ["text"]},
                    "supported_parameters": ["tools"],
                }
                for i in range(4)
            ]
            + [
                {
                    "id": "good/model",
                    "context_length": 128000,
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"},
                    "architecture": {"input_modalities": ["text"]},
                    "supported_parameters": ["tools"],
                }
            ]
        }
        resolver = OpenRouterModelMetadataResolver(_client(catalog))
        # Force the index build.
        assert resolver.resolve("good/model") is not None

        summaries = [
            r
            for r in log_records
            if r["level"].name == "WARNING"
            and r["message"] == "skipped openrouter catalog entries with incomplete metadata"
        ]
        # Exactly ONE summary WARNING for all 4 skipped entries.
        assert len(summaries) == 1
        assert summaries[0]["extra"]["skipped"] == 4
        assert summaries[0]["extra"]["indexed"] == 1
        # And no per-entry WARNING spam (the old behaviour was 4 WARNINGs).
        per_entry_warnings = [
            r
            for r in log_records
            if r["level"].name == "WARNING"
            and r["message"] == "skipping openrouter catalog entry with invalid metadata"
        ]
        assert per_entry_warnings == []


@pytest.fixture
def log_records() -> Iterator[list[dict[str, Any]]]:
    """Loguru sink capturing full records (message + level + structured ``extra``)."""
    records: list[dict[str, Any]] = []
    sink_id = _loguru_logger.add(lambda m: records.append(dict(m.record)), level="DEBUG")
    try:
        yield records
    finally:
        _loguru_logger.remove(sink_id)
