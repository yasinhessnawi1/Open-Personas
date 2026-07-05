"""Spec P9 routing policy — deliberate surface→tier, stated once (P9-D-1).

The router stops guessing. A turn's tier is a function of its SURFACE
(chat / voice / background / recognition / authoring / agentic_step), which
the caller already knows — never of turn counts or keyword-matching the
message. :data:`SURFACE_TIER_POLICY` is the whole policy, stated once:

* ``chat`` / ``authoring`` / ``agentic_step`` → ``frontier`` — quality is the
  default for anything the user reads.
* ``voice`` → ``mid`` — **the latency tier**. A latency constraint, not a
  cost preference: measured 2026-07-04 (P9-R-2), no configured frontier model
  fits the 800 ms P50 voice budget on the model hop alone (GLM 5.2 median
  TTFT 6 565 ms; claude-sonnet-4-6 857 ms; Groq mid 57 ms). A future
  fast-frontier model swaps in via ``PERSONA_MID_MODELS`` or one table edit —
  re-measure against the budget first.
* ``recognition`` → ``mid`` — the A4/A8 intent interpreters gate whether the
  persona can ACT; small was the measured confabulation root AND is slower
  than mid (P9-D-2). Env-overridable upward via
  ``PERSONA_API_RECOGNITION_TIER`` (the override arrives through
  :func:`tier_for`'s ``override`` parameter — the runtime stays env-free;
  the API composition root owns env reads).
* ``background`` → ``small`` — synthesis, titles, summaries: narrow, unread,
  high-volume, latency-insensitive.

:class:`PolicyRouter` carries the policy through the Spec 18
:class:`~persona_runtime.routing.protocol.Router` Protocol so the
:class:`~persona_runtime.loop.ConversationLoop` seam is unchanged: the
composition roots swap ``Router()`` for ``PolicyRouter(tier_registry=...)``
and nothing else moves. The persona pin (``routing.tier_for_generation``)
stays a composition-root short-circuit exactly as before — a deliberate
override is honored; the retired heuristics are not consulted (P9-D-6/D-7).

Layer 1 (:func:`~persona_runtime.routing.layer1.apply_constraint_filter`)
still runs first: **constraint beats preference** — a vision turn keeps the
Spec 13 fail-loud guarantee, and a filtered-out policy tier degrades to the
first surviving candidate rather than violating a hard constraint.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

from persona_runtime.routing import layer1
from persona_runtime.routing.types import RoutingDecision

if TYPE_CHECKING:
    from collections.abc import Mapping

    from persona_runtime.routing.types import RoutingContext, RoutingProfile
    from persona_runtime.tier import TierRegistry

__all__ = [
    "SURFACE_TIER_POLICY",
    "PolicyRouter",
    "Surface",
    "surface_for_profile",
    "tier_for",
]


Surface = Literal["chat", "voice", "background", "recognition", "authoring", "agentic_step"]
"""The P9-D-1 surface set — every model-calling job maps to exactly one.

``agentic_step`` is its own row (not an alias of ``chat``) because its
composition site (:meth:`AgenticLoop._tier_for_step`) is not profile-driven;
per the Phase 1 gate ruling, agentic steps produce user-read run output and
route frontier.
"""


SURFACE_TIER_POLICY: Final[Mapping[Surface, str]] = MappingProxyType(
    {
        "chat": "frontier",
        "voice": "mid",  # THE LATENCY TIER — see module docstring (P9-D-3)
        "background": "small",
        "recognition": "mid",  # P9-D-2 — min viable for intent parsing
        "authoring": "frontier",  # incl. the tool/capability recommenders
        "agentic_step": "frontier",  # user-read run output
    }
)
"""The policy table. One place, one statement, no inference."""


_PROFILE_SURFACE: Final[Mapping[str, Surface]] = MappingProxyType(
    {
        "text_default": "chat",
        "voice": "voice",
    }
)
"""Maps :data:`~persona_runtime.routing.types.RoutingProfile` → surface.

Extending ``RoutingProfile`` with a new literal requires a row here —
:class:`PolicyRouter.route` fails loud (``KeyError``) on an unmapped profile
rather than silently guessing a tier; the exhaustiveness test at
``test_routing_policy.py`` keeps the two literals in lock-step.
"""


def tier_for(
    surface: Surface,
    *,
    pin: str | None = None,
    override: str | None = None,
) -> str:
    """Resolve the tier for ``surface`` (P9-D-1).

    Precedence: **pin > override > policy table**.

    Args:
        surface: The calling job's surface.
        pin: The persona's ``routing.tier_for_generation`` where the surface
            honors persona pins (chat / voice generation). ``None`` or
            ``"auto"`` means no pin — the deliberate-override half of P9-D-7:
            a stored pin is a real user choice and wins outright.
        override: An operator-configured surface tier injected by the
            composition root (e.g. ``config.recognition_tier`` from
            ``PERSONA_API_RECOGNITION_TIER``). Beats the table, loses to a
            pin. Values are passed through unvalidated — an unknown tier
            name fails fast downstream at
            :meth:`~persona_runtime.tier.TierRegistry.get` with
            :class:`~persona_runtime.tier.TierNotConfiguredError`.

    Returns:
        The tier name to run this surface on.
    """
    if pin is not None and pin != "auto":
        return pin
    if override is not None:
        return override
    return SURFACE_TIER_POLICY[surface]


class PolicyRouter:
    """The P9 policy behind the Spec 18 Router Protocol (P9-D-1).

    Drop-in for the router injection points (`ConversationLoop`,
    `AgenticLoop`, the voice worker): maps the context's profile to a
    surface, applies Layer 1, returns the policy tier. Deterministic,
    dependency-free, sub-1ms — the same operational envelope as
    :class:`~persona_runtime.routing.heuristic.HeuristicRouter`, without the
    turn-count / keyword rules (P9-D-6: the context's ``is_boilerplate`` /
    ``is_identity_sensitive`` signals are ignored for tier choice; they stay
    on the TurnLog for observability).

    The persona-pin short-circuit remains the composition root's
    responsibility (`ConversationLoop._decide_routing` /
    `ReplyProducer._choose_tier`) — this router never sees a pinned turn,
    mirroring the Spec 05/18 contract.

    Args:
        tier_registry: The deployment's registry. When provided, Layer 1
            enforces real constraints and the decision carries the resolved
            model name. When ``None`` (unit-test path), candidates are the
            canonical ``("frontier", "mid", "small")`` and ``model=""``.
    """

    def __init__(self, tier_registry: TierRegistry | None = None) -> None:
        self._tier_registry = tier_registry

    def route(self, context: RoutingContext) -> RoutingDecision:
        """Return the policy decision for the turn described by ``context``.

        Args:
            context: The turn's routing context; only ``profile`` and the
                Layer 1 hard-constraint signals influence the outcome.

        Returns:
            A :class:`~persona_runtime.routing.types.RoutingDecision` whose
            rationale names the policy (``"policy: chat → frontier"``), or
            the constraint degradation when Layer 1 filtered the policy tier
            out (``"policy: chat → frontier; constrained → mid"``).

        Raises:
            NoVisionTierConfiguredError: vision turn, no vision-capable tier
                (Spec 13 fail-loud, via Layer 1).
            RoutingConstraintsUnsatisfiableError: a hard constraint emptied
                the candidate set (via Layer 1).
        """
        surface = _PROFILE_SURFACE[context.profile]
        candidates = layer1.apply_constraint_filter(context, self._tier_registry)
        preferred = tier_for(surface)

        if preferred in candidates:
            tier = preferred
            rationale = f"policy: {surface} → {tier}"
        else:
            # Constraint beats preference — degrade to the first surviving
            # candidate (preserves the configured / vision-filtered order).
            tier = candidates[0]
            rationale = f"policy: {surface} → {preferred}; constrained → {tier}"

        return RoutingDecision(
            tier=tier,
            model=self._resolve_model(tier),
            rationale=rationale,
            candidates_considered=candidates,
            layer1_filter_reasons={},
            layer2_score=0.0,
        )

    def _resolve_model(self, tier: str) -> str:
        """Return the registry's model name for ``tier``, or ``""`` when absent."""
        if self._tier_registry is None:
            return ""
        return self._tier_registry.model_name_for(tier)


def surface_for_profile(profile: RoutingProfile) -> Surface:
    """Return the surface a routing profile maps to (exposed for tests/tools)."""
    return _PROFILE_SURFACE[profile]
