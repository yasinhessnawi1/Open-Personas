"""Spec 18 — Unified model router.

The routing package replaces Spec 05's concrete :class:`Router` class with a
pluggable :class:`Router` Protocol. Two implementations behind it:

* :class:`HeuristicRouter` — the Spec 05 rules, byte-for-byte preserved.
* :class:`UnifiedRouter` — Layer 1 capability hard filter + Layer 2 sweet-spot
  scorer over cost / quality / latency, weighted per :data:`RoutingProfile`.

The ``voice`` profile subsumes V5's voice-latency routing — one router, two
modalities. Layer 1 is shared between both implementations via the free
function :func:`apply_constraint_filter` (D-18-X-layer1-extraction).

T02 ships the boundary types only:

* :class:`RoutingContext` — facts the router reasons over.
* :class:`RoutingDecision` — what the router returned, carried on TurnLog.
* :data:`RoutingProfile` — the profile literal driving Layer 2 weights.

T03 ships the :class:`Router` Protocol + the constraint-failure error. T04
extends the TierRegistry with cost/latency metadata. T05 refactors the Spec 05
:class:`Router` into :class:`HeuristicRouter` behind the Protocol. T09–T11
ship :class:`UnifiedRouter`. T12 lands the additive TurnLog extension.
"""

from __future__ import annotations

from persona.backends.errors import (
    NoVisionTierConfiguredError,
    RoutingConstraintsUnsatisfiableError,
)

from persona_runtime.routing.heuristic import HeuristicRouter
from persona_runtime.routing.intelligent_router import IntelligentRouter, ModelSelection
from persona_runtime.routing.latency import FirstTokenLatencyTracker
from persona_runtime.routing.layer1 import apply_constraint_filter
from persona_runtime.routing.model_selection import canonical_model_id, reorder_primary
from persona_runtime.routing.nvidia_models import (
    NVIDIA_LAUNCH_MODEL_METADATA,
    nvidia_metadata_for_model,
)
from persona_runtime.routing.policy import (
    SURFACE_TIER_POLICY,
    PolicyRouter,
    Surface,
    surface_for_profile,
    tier_for,
)
from persona_runtime.routing.protocol import Router, RouterScorer
from persona_runtime.routing.types import (
    RoutingContext,
    RoutingDecision,
    RoutingProfile,
)
from persona_runtime.routing.unified import UnifiedRouter

__all__ = [
    "FirstTokenLatencyTracker",
    "HeuristicRouter",
    "IntelligentRouter",
    "ModelSelection",
    "NVIDIA_LAUNCH_MODEL_METADATA",
    "NoVisionTierConfiguredError",
    "PolicyRouter",
    "Router",
    "RouterScorer",
    "RoutingConstraintsUnsatisfiableError",
    "RoutingContext",
    "RoutingDecision",
    "RoutingProfile",
    "SURFACE_TIER_POLICY",
    "Surface",
    "UnifiedRouter",
    "apply_constraint_filter",
    "canonical_model_id",
    "nvidia_metadata_for_model",
    "reorder_primary",
    "surface_for_profile",
    "tier_for",
]
