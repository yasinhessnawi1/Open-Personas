"""Unit tests for Spec 18 Layer 1 free function `apply_constraint_filter` (T09).

Covers the three hard filters (vision / context-window / strong-tools) +
graceful behaviour when metadata is absent + the structured-context shape
of :class:`RoutingConstraintsUnsatisfiableError` for each failure mode.

The Spec 13 vision-failure shape (preserved
:class:`NoVisionTierConfiguredError`) is verified at
``test_router_vision.py`` end-to-end; this file pins the new generalised
constraint-failure shapes T09 adds.
"""

from __future__ import annotations

import pytest
from persona.backends import BackendConfig
from persona.backends.errors import (
    NoVisionTierConfiguredError,
    RoutingConstraintsUnsatisfiableError,
)
from persona_runtime.errors import TierNotConfiguredError
from persona_runtime.routing import RoutingContext, apply_constraint_filter
from persona_runtime.tier import TierConfig, TierMetadata, TierRegistry


def _backend_cfg(model: str = "m") -> BackendConfig:
    return BackendConfig(provider="anthropic", model=model, api_key="sk-test")


def _metadata(
    *,
    cost_in: float = 0.3,
    cost_out: float = 1.5,
    latency: float = 800.0,
    throughput: float = 60.0,
    context_window: int = 200_000,
    tool: str = "strong",
) -> TierMetadata:
    return TierMetadata(
        cost_input_per_1k_tokens=cost_in,
        cost_output_per_1k_tokens=cost_out,
        first_token_latency_ms=latency,
        throughput_tokens_per_sec=throughput,
        context_window=context_window,
        tool_strength=tool,  # type: ignore[arg-type]
    )


def _context(
    *,
    requires_vision: bool = False,
    requires_strong_tools: bool = False,
    estimated_input_tokens: int = 100,
    profile: str = "text_default",
) -> RoutingContext:
    return RoutingContext(
        requires_vision=requires_vision,
        estimated_input_tokens=estimated_input_tokens,
        requires_strong_tools=requires_strong_tools,
        is_first_turn=False,
        is_identity_sensitive=False,
        is_boilerplate=False,
        conversation_phase="middle",
        profile=profile,  # type: ignore[arg-type]
    )


def _registry_with_metadata(
    *tiers: tuple[str, str, bool, TierMetadata | None],
) -> TierRegistry:
    """Build a TierRegistry from (name, model, supports_vision, metadata) tuples.

    Pre-populates the cache with stub backends so ``supports_vision_for`` returns
    the supplied value without instantiating real backends.
    """

    class _StubBackend:
        def __init__(self, supports_vision: bool, model_name: str) -> None:
            self.supports_vision = supports_vision
            self.model_name = model_name

    registry = TierRegistry(
        {
            name: TierConfig(name=name, backend_config=_backend_cfg(model), metadata=md)
            for name, model, _vision, md in tiers
        }
    )
    registry._cache = {  # type: ignore[assignment]  # noqa: SLF001
        name: _StubBackend(supports_vision, model) for name, model, supports_vision, _ in tiers
    }
    return registry


# ----- No registry path -----------------------------------------------------


class TestNoRegistry:
    def test_returns_canonical_default_set(self) -> None:
        result = apply_constraint_filter(_context(), None)
        assert result == ("frontier", "mid", "small")

    def test_canonical_set_even_with_vision_required(self) -> None:
        # Legacy unit-test path — no constraint enforcement when registry absent.
        result = apply_constraint_filter(_context(requires_vision=True), None)
        assert result == ("frontier", "mid", "small")


# ----- Empty tier registry (R9-059) -----------------------------------------


class TestEmptyTierRegistry:
    def test_empty_registry_raises_tier_not_configured_not_routing_error(self) -> None:
        """An empty TierRegistry (e.g. a cloud free-plan caller with unconfigured
        ``PERSONA_FREE_*_MODELS``, M4) must raise the GRACEFUL, HANDLED
        ``TierNotConfiguredError`` — never the misleading
        ``RoutingConstraintsUnsatisfiableError`` (which has no registered app
        handler and previously 500'd, mislabelled ``context_window_exceeded``)."""
        registry = TierRegistry({})
        with pytest.raises(TierNotConfiguredError) as excinfo:
            apply_constraint_filter(_context(), registry)
        # Not the routing-constraints error, and not mislabelled as a window problem.
        assert not isinstance(excinfo.value, RoutingConstraintsUnsatisfiableError)
        assert excinfo.value.context.get("reason") != "context_window_exceeded"

    def test_empty_registry_raises_even_for_a_vision_turn(self) -> None:
        # The empty-registry check fires BEFORE the vision filter — same clear
        # signal regardless of which constraint the turn would have hit next.
        registry = TierRegistry({})
        with pytest.raises(TierNotConfiguredError):
            apply_constraint_filter(_context(requires_vision=True), registry)


# ----- Vision constraint ---------------------------------------------------


class TestVisionConstraint:
    def test_non_vision_turn_keeps_all_tiers(self) -> None:
        registry = _registry_with_metadata(
            ("frontier", "opus", True, None),
            ("small", "llama", False, None),
        )
        result = apply_constraint_filter(_context(requires_vision=False), registry)
        assert set(result) == {"frontier", "small"}

    def test_vision_turn_excludes_text_only_tier(self) -> None:
        registry = _registry_with_metadata(
            ("frontier", "opus", True, None),
            ("small", "llama", False, None),
        )
        result = apply_constraint_filter(_context(requires_vision=True), registry)
        assert result == ("frontier",)

    def test_vision_turn_with_no_vision_tier_raises(self) -> None:
        registry = _registry_with_metadata(
            ("mid", "haiku", False, None),
            ("small", "llama", False, None),
        )
        with pytest.raises(NoVisionTierConfiguredError) as excinfo:
            apply_constraint_filter(_context(requires_vision=True), registry)
        assert excinfo.value.context["reason"] == "no_vision_tier"
        assert excinfo.value.context["configured_tiers"] == "mid,small"

    def test_no_vision_error_is_subclass_of_constraints_unsatisfiable(self) -> None:
        registry = _registry_with_metadata(
            ("mid", "haiku", False, None),
        )
        with pytest.raises(RoutingConstraintsUnsatisfiableError):
            apply_constraint_filter(_context(requires_vision=True), registry)


# ----- Context-window constraint -------------------------------------------


class TestContextWindowConstraint:
    def test_under_window_keeps_all_tiers(self) -> None:
        registry = _registry_with_metadata(
            ("frontier", "opus", False, _metadata(context_window=200_000)),
            ("mid", "haiku", False, _metadata(context_window=100_000)),
            ("small", "llama", False, _metadata(context_window=8_000)),
        )
        result = apply_constraint_filter(_context(estimated_input_tokens=5_000), registry)
        assert set(result) == {"frontier", "mid", "small"}

    def test_exceeds_some_windows_excludes_them(self) -> None:
        registry = _registry_with_metadata(
            ("frontier", "opus", False, _metadata(context_window=200_000)),
            ("mid", "haiku", False, _metadata(context_window=100_000)),
            ("small", "llama", False, _metadata(context_window=8_000)),
        )
        result = apply_constraint_filter(_context(estimated_input_tokens=50_000), registry)
        # small (8k) excluded; mid (100k) + frontier (200k) survive.
        assert set(result) == {"frontier", "mid"}

    def test_metadata_absent_gracefully_passes(self) -> None:
        registry = _registry_with_metadata(
            ("frontier", "opus", False, _metadata(context_window=200_000)),
            ("mid", "haiku", False, None),  # no metadata → graceful pass
        )
        result = apply_constraint_filter(_context(estimated_input_tokens=500_000), registry)
        # frontier (200k) excluded; mid (no metadata) passes through gracefully.
        assert result == ("mid",)

    def test_exceeds_all_windows_raises(self) -> None:
        registry = _registry_with_metadata(
            ("mid", "haiku", False, _metadata(context_window=8_000)),
            ("small", "llama", False, _metadata(context_window=4_000)),
        )
        with pytest.raises(RoutingConstraintsUnsatisfiableError) as excinfo:
            apply_constraint_filter(_context(estimated_input_tokens=50_000), registry)
        assert excinfo.value.context["reason"] == "context_window_exceeded"
        assert excinfo.value.context["required"] == "context_window>=50000"


# ----- Tool-strength constraint --------------------------------------------


class TestToolStrengthConstraint:
    def test_not_required_keeps_all_tiers(self) -> None:
        registry = _registry_with_metadata(
            ("frontier", "opus", False, _metadata(tool="strong")),
            ("mid", "haiku", False, _metadata(tool="medium")),
            ("small", "llama", False, _metadata(tool="weak")),
        )
        result = apply_constraint_filter(_context(requires_strong_tools=False), registry)
        assert set(result) == {"frontier", "mid", "small"}

    def test_required_excludes_weak_and_medium(self) -> None:
        registry = _registry_with_metadata(
            ("frontier", "opus", False, _metadata(tool="strong")),
            ("mid", "haiku", False, _metadata(tool="medium")),
            ("small", "llama", False, _metadata(tool="weak")),
        )
        result = apply_constraint_filter(_context(requires_strong_tools=True), registry)
        assert result == ("frontier",)

    def test_required_with_no_strong_tier_raises(self) -> None:
        registry = _registry_with_metadata(
            ("mid", "haiku", False, _metadata(tool="medium")),
            ("small", "llama", False, _metadata(tool="weak")),
        )
        with pytest.raises(RoutingConstraintsUnsatisfiableError) as excinfo:
            apply_constraint_filter(_context(requires_strong_tools=True), registry)
        assert excinfo.value.context["reason"] == "no_strong_tools_tier"
        assert excinfo.value.context["required"] == "strong_tools"

    def test_required_with_metadata_absent_gracefully_passes(self) -> None:
        registry = _registry_with_metadata(
            ("frontier", "opus", False, None),  # no metadata → graceful
            ("mid", "haiku", False, _metadata(tool="medium")),
        )
        result = apply_constraint_filter(_context(requires_strong_tools=True), registry)
        # mid (medium) excluded; frontier (no metadata) passes through.
        assert result == ("frontier",)


# ----- Combined constraints -----------------------------------------------


class TestCombinedConstraints:
    def test_vision_plus_context_window(self) -> None:
        registry = _registry_with_metadata(
            ("frontier", "opus", True, _metadata(context_window=200_000)),
            ("mid", "haiku", True, _metadata(context_window=8_000)),
            ("small", "llama", False, _metadata(context_window=8_000)),
        )
        result = apply_constraint_filter(
            _context(requires_vision=True, estimated_input_tokens=50_000),
            registry,
        )
        # small excluded by vision; mid excluded by context window; frontier survives.
        assert result == ("frontier",)
