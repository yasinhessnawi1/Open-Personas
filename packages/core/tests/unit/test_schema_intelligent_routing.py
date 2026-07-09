"""Spec 23 T9 — persona schema extension (routing.intelligent + routing.budget).

D-23-9: additive optional blocks, NO schema_version bump. Personas without the
blocks load byte-identically (criterion 11).
"""

from __future__ import annotations

import pytest
from persona.schema.persona import (
    IntelligentRoutingConfig,
    Persona,
    RoutingBudgetConfig,
    RoutingConfig,
)
from pydantic import ValidationError


def _persona(**routing_kwargs: object) -> Persona:
    return Persona(
        identity={"name": "A", "role": "r", "background": "b"},  # type: ignore[arg-type]
        routing=RoutingConfig(**routing_kwargs),  # type: ignore[arg-type]
    )


class TestBackwardCompat:
    def test_persona_without_routing_block_defaults_intelligent_off(self) -> None:
        # Criterion 11: a persona authored before Spec 23 (no routing.* at all).
        p = Persona(identity={"name": "A", "role": "r", "background": "b"})  # type: ignore[arg-type]
        assert p.routing.intelligent.enabled is False
        assert p.routing.budget.max_cents_per_turn is None
        # schema_version untouched (D-23-9: no bump).
        assert p.schema_version == "1.0"

    def test_legacy_routing_construction_still_valid(self) -> None:
        # Old RoutingConfig(tier_for_generation=..., tier_for_tools=...) form.
        p = _persona(tier_for_generation="frontier", tier_for_tools="small")
        assert p.routing.tier_for_generation == "frontier"
        assert p.routing.intelligent.enabled is False

    def test_no_schema_version_bump(self) -> None:
        from persona.schema.persona import SUPPORTED_SCHEMA_VERSIONS

        assert frozenset({"1.0"}) == SUPPORTED_SCHEMA_VERSIONS


class TestIntelligentBlock:
    def test_enable_with_custom_weights(self) -> None:
        cfg = IntelligentRoutingConfig(
            enabled=True,
            weights={"cost": 0.8, "quality": 0.1, "latency": 0.1},  # type: ignore[arg-type]
        )
        assert cfg.enabled is True
        assert cfg.weights.cost == 0.8
        assert cfg.fallback_to_rule_based_on_miss is True

    def test_weights_default_to_text_default_profile(self) -> None:
        cfg = IntelligentRoutingConfig()
        assert (cfg.weights.cost, cfg.weights.quality, cfg.weights.latency) == (0.40, 0.50, 0.10)

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(ValidationError):
            IntelligentRoutingConfig(weights={"cost": -0.1})  # type: ignore[arg-type]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            IntelligentRoutingConfig(bogus=True)  # type: ignore[call-arg]


class TestBudgetBlock:
    def test_caps_default_none(self) -> None:
        b = RoutingBudgetConfig()
        assert b.max_cents_per_turn is None
        assert b.max_cents_per_session is None
        assert b.max_cents_per_day is None

    def test_caps_set(self) -> None:
        b = RoutingBudgetConfig(max_cents_per_turn=5.0, max_cents_per_session=100.0)
        assert b.max_cents_per_turn == 5.0
        assert b.max_cents_per_session == 100.0

    def test_negative_cap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RoutingBudgetConfig(max_cents_per_turn=-1.0)


class TestFullRoutingFromYaml:
    def test_round_trip_via_model_validate(self) -> None:
        raw = {
            "identity": {"name": "A", "role": "r", "background": "b"},
            "routing": {
                "tier_for_generation": "auto",
                "intelligent": {"enabled": True, "weights": {"cost": 0.6}},
                "budget": {"max_cents_per_turn": 3.0},
            },
        }
        p = Persona.model_validate(raw)
        assert p.routing.intelligent.enabled is True
        assert p.routing.intelligent.weights.cost == 0.6
        assert p.routing.intelligent.weights.quality == 0.50  # default preserved
        assert p.routing.budget.max_cents_per_turn == 3.0


class TestPreferredModel:
    def test_routing_preferred_model_defaults_none_and_old_yaml_loads(self) -> None:
        # Additive invariant: a pre-M1 routing block loads byte-identically.
        cfg = RoutingConfig()
        assert cfg.preferred_model is None

    def test_routing_preferred_model_accepts_canonical_id(self) -> None:
        cfg = RoutingConfig(preferred_model="anthropic/claude-sonnet-4.6")
        assert cfg.preferred_model == "anthropic/claude-sonnet-4.6"

    def test_routing_preferred_model_rejects_blank_and_whitespace(self) -> None:
        with pytest.raises(ValidationError):
            RoutingConfig(preferred_model="")
        with pytest.raises(ValidationError):
            RoutingConfig(preferred_model="   ")
