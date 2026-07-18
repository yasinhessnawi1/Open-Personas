"""The machine pricing registry (Spec M3, T1a — D-M3-5)."""

from __future__ import annotations

import pytest
from persona.billing.formula import BillingConfig
from persona.billing.pricing_registry import (
    PRICING_ROWS,
    infra_rate_cents,
    registry_keys,
)


def test_registry_keys_are_unique_and_cover_every_row() -> None:
    keys = registry_keys()
    assert len(keys) == len(PRICING_ROWS), "duplicate (surface, sku) in the registry"


def test_every_audited_surface_is_present() -> None:
    surfaces = {row.surface for row in PRICING_ROWS}
    assert {
        "chat",
        "authoring",
        "image",
        "voice_stt",
        "voice_tts",
        "voice_transport",
        "embeddings",
        "connectors_mcp",
        "sandbox",
    } <= surfaces


def test_llm_surfaces_defer_provider_cost_to_the_resolver() -> None:
    # chat + authoring price against the resolver chain / OpenRouter actual, not
    # a table constant here (provider_cost_cents is None).
    by_surface = {row.surface: row for row in PRICING_ROWS}
    assert by_surface["chat"].provider_cost_cents is None
    assert by_surface["authoring"].provider_cost_cents is None


def test_zero_provider_surfaces_carry_infra_flat_basis() -> None:
    by_surface = {row.surface: row for row in PRICING_ROWS}
    for surface in ("voice_transport", "embeddings", "connectors_mcp", "sandbox"):
        assert by_surface[surface].provider_cost_cents == pytest.approx(0.0)
        assert by_surface[surface].cost_basis == "infra_flat"


def test_infra_rate_cents_maps_each_unit_to_its_config_field() -> None:
    cfg = BillingConfig()
    assert infra_rate_cents(cfg, "llm_call") == pytest.approx(0.1)
    assert infra_rate_cents(cfg, "image") == pytest.approx(1.0)
    assert infra_rate_cents(cfg, "voice_min") == pytest.approx(1.0)
    assert infra_rate_cents(cfg, "embed_batch") == pytest.approx(0.1)
    assert infra_rate_cents(cfg, "tool_call") == pytest.approx(0.1)
    assert infra_rate_cents(cfg, "sandbox_exec") == pytest.approx(1.0)
    assert infra_rate_cents(cfg, "none") == pytest.approx(0.0)


def test_pricing_row_is_frozen() -> None:
    row = PRICING_ROWS[0]
    with pytest.raises((TypeError, ValueError)):
        row.surface = "mutated"  # type: ignore[misc]
