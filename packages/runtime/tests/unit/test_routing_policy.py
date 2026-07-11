"""Unit tests for the Spec P9 policy resolver + PolicyRouter (P9-D-1).

Locks the policy table (surface→tier, stated once), the `tier_for` precedence
(pin > override > table, P9-D-7's deliberate-override half), and
`PolicyRouter`'s Protocol behaviour: profile→surface mapping, classifier
signals ignored for tier choice (P9-D-6), constraint-beats-preference via
Layer 1 (vision fail-loud preserved), and the Router Protocol conformance
that makes it a drop-in at the composition roots.
"""

from __future__ import annotations

from typing import get_args

import pytest
from persona.backends import BackendConfig
from persona.backends.errors import NoVisionTierConfiguredError
from persona_runtime.routing import (
    SURFACE_TIER_POLICY,
    PolicyRouter,
    Router,
    RoutingContext,
    RoutingProfile,
    Surface,
    surface_for_profile,
    tier_for,
)
from persona_runtime.tier import TierConfig, TierRegistry


def _backend_cfg(model: str = "m") -> BackendConfig:
    return BackendConfig(provider="anthropic", model=model, api_key="sk-test")


def _context(
    *,
    requires_vision: bool = False,
    is_first_turn: bool = False,
    is_identity_sensitive: bool = False,
    is_boilerplate: bool = False,
    profile: str = "text_default",
) -> RoutingContext:
    return RoutingContext(
        requires_vision=requires_vision,
        estimated_input_tokens=100,
        requires_strong_tools=False,
        is_first_turn=is_first_turn,
        is_identity_sensitive=is_identity_sensitive,
        is_boilerplate=is_boilerplate,
        conversation_phase="middle",
        profile=profile,  # type: ignore[arg-type]
    )


def _registry(*tiers: tuple[str, str, bool]) -> TierRegistry:
    """Build a TierRegistry from (name, model, supports_vision) tuples."""

    class _StubBackend:
        def __init__(self, supports_vision: bool, model_name: str) -> None:
            self.supports_vision = supports_vision
            self.model_name = model_name

    registry = TierRegistry(
        {
            name: TierConfig(name=name, backend_config=_backend_cfg(model))
            for name, model, _vision in tiers
        }
    )
    registry._cache = {  # type: ignore[assignment]  # noqa: SLF001
        name: _StubBackend(vision, model) for name, model, vision in tiers
    }
    return registry


# ----- The policy table (the statement, locked) -----------------------------


class TestPolicyTable:
    def test_chat_is_frontier(self) -> None:
        assert SURFACE_TIER_POLICY["chat"] == "frontier"

    def test_voice_is_the_latency_tier_mid(self) -> None:
        assert SURFACE_TIER_POLICY["voice"] == "mid"

    def test_background_is_small(self) -> None:
        assert SURFACE_TIER_POLICY["background"] == "small"

    def test_recognition_is_mid_never_small(self) -> None:
        # The confabulation root closed at the tier level (P9-D-2).
        assert SURFACE_TIER_POLICY["recognition"] == "mid"

    def test_authoring_is_frontier(self) -> None:
        assert SURFACE_TIER_POLICY["authoring"] == "frontier"

    def test_agentic_step_is_frontier(self) -> None:
        # User-read run output (Phase 1 gate ruling).
        assert SURFACE_TIER_POLICY["agentic_step"] == "frontier"

    def test_title_is_mid_never_small(self) -> None:
        # R9-020: titles are user-read chrome; small was the echo→first-words
        # fallback root (the R4 sanitizer fired on ~every conversation). This
        # guard FAILS if anyone re-pins titles to small — that re-opens the bug.
        assert SURFACE_TIER_POLICY["title"] == "mid"

    def test_title_override_beats_the_table(self) -> None:
        # The PERSONA_API_TITLE_TIER plumbing (the recognition precedent).
        assert tier_for("title") == "mid"
        assert tier_for("title", override="frontier") == "frontier"

    def test_table_is_exhaustive_over_the_surface_literal(self) -> None:
        assert set(SURFACE_TIER_POLICY) == set(get_args(Surface))

    def test_table_is_immutable(self) -> None:
        with pytest.raises(TypeError):
            SURFACE_TIER_POLICY["chat"] = "small"  # type: ignore[index]

    def test_every_routing_profile_maps_to_a_surface(self) -> None:
        # Extending RoutingProfile without a mapping row must fail THIS test,
        # not silently misroute in production.
        for profile in get_args(RoutingProfile):
            assert surface_for_profile(profile) in SURFACE_TIER_POLICY


# ----- tier_for precedence: pin > override > table --------------------------


class TestTierForPrecedence:
    def test_default_is_the_table(self) -> None:
        assert tier_for("chat") == "frontier"
        assert tier_for("recognition") == "mid"

    def test_override_beats_the_table(self) -> None:
        assert tier_for("recognition", override="frontier") == "frontier"

    def test_pin_beats_the_override_and_the_table(self) -> None:
        assert tier_for("chat", pin="mid", override="small") == "mid"

    def test_auto_pin_means_no_pin(self) -> None:
        assert tier_for("chat", pin="auto") == "frontier"
        assert tier_for("chat", pin="auto", override="mid") == "mid"

    def test_pin_holds_a_persona_below_the_frontier_default(self) -> None:
        # Back-compat both ways (criterion 1): a stored mid pin stays mid.
        assert tier_for("chat", pin="mid") == "mid"

    def test_pin_lifts_a_surface_above_its_default(self) -> None:
        assert tier_for("voice", pin="frontier") == "frontier"


# ----- PolicyRouter: deterministic, signal-blind, Protocol-conformant -------


class TestPolicyRouterChat:
    def test_chat_resolves_frontier(self) -> None:
        decision = PolicyRouter().route(_context())
        assert decision.tier == "frontier"
        assert decision.rationale == "policy: chat → frontier"

    def test_first_turn_signal_does_not_matter(self) -> None:
        # No turn-1-only frontier: turn 1 and turn N are identical.
        first = PolicyRouter().route(_context(is_first_turn=True))
        later = PolicyRouter().route(_context(is_first_turn=False))
        assert first.tier == later.tier == "frontier"

    def test_boilerplate_signal_is_ignored(self) -> None:
        # P9-D-6: "thanks" no longer downgrades the turn to small.
        decision = PolicyRouter().route(_context(is_boilerplate=True))
        assert decision.tier == "frontier"

    def test_identity_sensitive_signal_is_ignored(self) -> None:
        decision = PolicyRouter().route(_context(is_identity_sensitive=True))
        assert decision.tier == "frontier"

    def test_no_registry_yields_empty_model_and_canonical_candidates(self) -> None:
        decision = PolicyRouter().route(_context())
        assert decision.model == ""
        assert decision.candidates_considered == ("frontier", "mid", "small")

    def test_registry_resolves_the_model_name(self) -> None:
        registry = _registry(("frontier", "opus", True), ("mid", "sonnet", True))
        decision = PolicyRouter(tier_registry=registry).route(_context())
        assert decision.tier == "frontier"
        assert decision.model == "opus"


class TestPolicyRouterVoice:
    def test_voice_resolves_the_latency_tier(self) -> None:
        decision = PolicyRouter().route(_context(profile="voice"))
        assert decision.tier == "mid"
        assert decision.rationale == "policy: voice → mid"

    def test_voice_first_turn_is_not_frontier(self) -> None:
        # The bare-Router() turn-1 frontier hazard is gone (P9-D-3, intended
        # behaviour change — the model hop must fit the 800ms voice budget).
        decision = PolicyRouter().route(_context(profile="voice", is_first_turn=True))
        assert decision.tier == "mid"


class TestPolicyRouterConstraints:
    def test_vision_turn_keeps_the_policy_tier_when_capable(self) -> None:
        registry = _registry(("frontier", "opus", True), ("mid", "llama", False))
        decision = PolicyRouter(tier_registry=registry).route(_context(requires_vision=True))
        assert decision.tier == "frontier"

    def test_vision_filter_beats_the_policy_preference(self) -> None:
        # Frontier is text-only here — constraint beats preference; the
        # decision degrades to the surviving candidate and says so.
        registry = _registry(("frontier", "glm", False), ("mid", "gpt4o", True))
        decision = PolicyRouter(tier_registry=registry).route(_context(requires_vision=True))
        assert decision.tier == "mid"
        assert decision.rationale == "policy: chat → frontier; constrained → mid"

    def test_no_vision_tier_stays_fail_loud(self) -> None:
        # Spec 13's guarantee survives P9 (criterion: constraint fail-loud).
        registry = _registry(("frontier", "glm", False), ("mid", "llama", False))
        with pytest.raises(NoVisionTierConfiguredError):
            PolicyRouter(tier_registry=registry).route(_context(requires_vision=True))


class TestProtocolConformance:
    def test_policy_router_satisfies_the_router_protocol(self) -> None:
        assert isinstance(PolicyRouter(), Router)

    def test_decision_is_heuristic_shaped_for_the_turnlog(self) -> None:
        # No Layer 2 score, no fallback flags — the TurnLog consumers see the
        # same shape HeuristicRouter emitted (no boundary change).
        decision = PolicyRouter().route(_context())
        assert decision.layer2_score == 0.0
        assert decision.fallback_triggered is False
        assert decision.layer1_filter_reasons == {}
