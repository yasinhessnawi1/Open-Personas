"""Spec 18 Layer 1 — capability hard filter (T09; D-18-X-layer1-extraction).

A **free function** consumed by BOTH :class:`HeuristicRouter` and
:class:`UnifiedRouter` via import. The shared-by-import pattern prevents the
"parallel Layer 1 implementations drift" failure mode — there is literally
one Layer 1 implementation, not one per router class. T08's invariant test
patches :data:`apply_constraint_filter` at this module's level and verifies
both router types call through.

**Constraint enforcement** — three hard filters applied in order:

1. **Vision** (Spec 13 + ``context.requires_vision``) — vision-required turns
   filtered to vision-capable tiers. Empty result raises
   :class:`NoVisionTierConfiguredError` (the Spec 13 subclass preserves the
   existing structured context shape: ``reason="no_vision_tier"``).
2. **Context window** (``context.estimated_input_tokens`` vs
   ``metadata.context_window``) — turns whose estimated token count exceeds
   a tier's window are excluded from that tier. **Graceful when metadata
   is absent** — tiers without metadata pass the filter (D-18-X-partial-metadata-behaviour
   handles "did Layer 2 score these?" downstream).
3. **Tool strength** (``context.requires_strong_tools`` vs
   ``metadata.tool_strength``) — when strong tools are required, only tiers
   with ``tool_strength="strong"`` survive. Graceful when metadata absent.

Empty filter set after constraints other than vision raises
:class:`RoutingConstraintsUnsatisfiableError` with the reason that fired.

An empty **starting** tier set (no tier configured at all — e.g. a cloud
free-plan caller handed an empty free ``TierRegistry`` per M4, R9-059) is
a distinct failure mode from any of the three constraints above and is
checked first: it raises :class:`TierNotConfiguredError`, the same
exception :meth:`TierRegistry.get` raises, so it rides the app's existing
graceful 503 + upgrade-CTA handler instead of being misdiagnosed as
``context_window_exceeded`` (a `RoutingConstraintsUnsatisfiableError`
with no registered handler, which previously 500'd).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.backends.errors import (
    NoVisionTierConfiguredError,
    RoutingConstraintsUnsatisfiableError,
)

from persona_runtime.errors import TierNotConfiguredError

if TYPE_CHECKING:
    from persona_runtime.routing.types import RoutingContext
    from persona_runtime.tier import TierRegistry

__all__ = ["apply_constraint_filter"]


def apply_constraint_filter(
    context: RoutingContext,
    tier_registry: TierRegistry | None,
) -> tuple[str, ...]:
    """Apply Layer 1 hard filter — returns the surviving tier set.

    Args:
        context: The turn's :class:`~persona_runtime.routing.types.RoutingContext`
            carrying the hard requirement signals.
        tier_registry: The deployment's :class:`~persona_runtime.tier.TierRegistry`.
            When ``None``, returns the canonical ``("frontier", "mid", "small")``
            set (legacy-test path; no constraint enforcement).

    Returns:
        Ordered tuple of tier names that satisfy ALL hard constraints in
        ``context``. Ordering mirrors
        :attr:`TierRegistry.configured_tier_names`.

    Raises:
        TierNotConfiguredError: ``tier_registry`` is configured (non-``None``)
            but has an empty tier set — no tier is configured at all
            (R9-059). Distinct from the constraint failures below; caught by
            the app's existing graceful 503 + upgrade-CTA handler.
        NoVisionTierConfiguredError: ``context.requires_vision`` is ``True``
            and no configured tier is vision-capable (Spec 13 fail-loud,
            preserved structured-context shape).
        RoutingConstraintsUnsatisfiableError: Vision filter passed but a
            subsequent constraint (context window or strong tools) empties
            the candidate set. Carries
            ``{"reason", "configured_tiers", "required"}`` context.
    """
    if tier_registry is None:
        return ("frontier", "mid", "small")

    configured = tier_registry.configured_tier_names
    if not configured:
        raise TierNotConfiguredError(
            "no model tier is configured",
            context={
                "requested": "(any)",
                "configured": "(none)",
                "reason": "empty_tier_registry",
            },
        )
    filtered: tuple[str, ...] = configured

    # --- Vision constraint (Spec 13) ---
    if context.requires_vision:
        filtered = tuple(t for t in filtered if tier_registry.supports_vision_for(t))
        if not filtered:
            raise NoVisionTierConfiguredError(
                "no vision-capable tier is configured for this turn",
                context={
                    "reason": "no_vision_tier",
                    "configured_tiers": ",".join(configured),
                },
            )

    # --- Context-window constraint (graceful with absent metadata) ---
    pre_window = filtered
    filtered = tuple(
        t
        for t in filtered
        if (md := tier_registry.metadata_for(t)) is None
        or md.context_window >= context.estimated_input_tokens
    )
    if not filtered:
        raise RoutingConstraintsUnsatisfiableError(
            "no tier with adequate context window",
            context={
                "reason": "context_window_exceeded",
                "configured_tiers": ",".join(pre_window),
                "required": f"context_window>={context.estimated_input_tokens}",
            },
        )

    # --- Tool-strength constraint (graceful with absent metadata) ---
    if context.requires_strong_tools:
        pre_tools = filtered
        filtered = tuple(
            t
            for t in filtered
            if (md := tier_registry.metadata_for(t)) is None or md.tool_strength == "strong"
        )
        if not filtered:
            raise RoutingConstraintsUnsatisfiableError(
                "no tier with strong tool-calling capability",
                context={
                    "reason": "no_strong_tools_tier",
                    "configured_tiers": ",".join(pre_tools),
                    "required": "strong_tools",
                },
            )

    return filtered
