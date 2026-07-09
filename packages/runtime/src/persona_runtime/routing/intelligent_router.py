"""Spec 23 IntelligentRouter — model-within-tier selection (T10; D-23-X-seam-shape).

Composes with (does NOT replace) the rule-based tier router: the existing
:class:`~persona_runtime.routing.unified.UnifiedRouter` /
:class:`~persona_runtime.routing.heuristic.HeuristicRouter` still chooses the TIER
(frontier / mid / small) — §9 closed decision intact. The IntelligentRouter then
picks the best MODEL within that tier's MODELS list via deterministic metadata
scoring (T7) + budget enforcement (T8), and the loop applies the choice through the
cheap :func:`~persona_runtime.routing.model_selection.reorder_primary` seam.

It is invoked by the turn loop AFTER the tier decision (not through the
``Router.route`` Protocol) because model selection needs two inputs the
``RoutingContext`` does not carry: the persona's
:class:`~persona.schema.persona.IntelligentRoutingConfig` /
:class:`~persona.schema.persona.RoutingBudgetConfig`, and the loop-owned
per-session / per-day spend tally (D-23-7 — the tally lives in the loop, not here;
this component stays stateless).

Graceful degradation (criterion 9): a metadata miss, an empty capability set, or a
non-multi-model tier returns a :class:`ModelSelection` with
``fallback_engaged=True`` and the rule-based slot-0 model — the turn never crashes.
The ONLY fail-loud path is the per-turn HARD budget cap
(:class:`~persona.backends.errors.BudgetExceededError`, criterion 7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from persona.backends.errors import IntelligentRoutingError
from persona.logging import get_logger

from persona_runtime.routing import routing_budget
from persona_runtime.routing.model_scorer import score_models
from persona_runtime.routing.model_selection import canonical_model_id
from persona_runtime.routing.scoring import ProfileWeights

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.backends.model_metadata import ModelMetadata, ModelMetadataResolver
    from persona.schema.persona import IntelligentRoutingConfig, RoutingBudgetConfig

    from persona_runtime.routing.latency import FirstTokenLatencyTracker
    from persona_runtime.routing.model_scorer import Candidate
    from persona_runtime.routing.types import RoutingContext
    from persona_runtime.tier import TierRegistry

__all__ = ["IntelligentRouter", "ModelSelection"]

_LOG = get_logger("routing.intelligent")

# D-23-6: the live per-model latency tracker overrides the static
# ModelMetadata.latency_p50_ms only once a model has graduated to EWMA — i.e.
# ``sample_count >= warmup_n``. The tracker's sample_count saturates at warmup_n
# (5) post-graduation, so N=5 is the only observable threshold.
_LATENCY_MIN_SAMPLES = 5


@dataclass(frozen=True)
class ModelSelection:
    """The IntelligentRouter's per-turn model choice (T10).

    Maps directly onto the additive :class:`RoutingDecision` model-selection
    fields (D-23-X-routing-decision-extension) the loop carries onto the JSONL
    TurnLog (criterion 10).
    """

    model: str
    model_candidates: tuple[str, ...] = ()
    score_vector: dict[str, float] = field(default_factory=dict)
    weights_used: dict[str, float] = field(default_factory=dict)
    fallback_engaged: bool = False
    fallback_reason: str | None = None


class IntelligentRouter:
    """Pick the best model within an already-chosen tier (T10).

    Args:
        tier_registry: The deployment registry — its ``candidate_models_for``
            enumerates the tier's models without instantiating backends.
        metadata_resolver: The chained resolver (static → OpenRouter, D-23-5).
        latency_tracker: Optional live per-model first-token tracker (D-23-6).
            When a model has ≥ :data:`_LATENCY_MIN_SAMPLES` samples its observed
            latency overrides the static value; otherwise the static value holds.
    """

    def __init__(
        self,
        *,
        tier_registry: TierRegistry,
        metadata_resolver: ModelMetadataResolver,
        latency_tracker: FirstTokenLatencyTracker | None = None,
    ) -> None:
        self._registry = tier_registry
        self._resolver = metadata_resolver
        self._latency = latency_tracker

    def select_model(
        self,
        tier: str,
        context: RoutingContext,
        *,
        intelligent: IntelligentRoutingConfig,
        budget: RoutingBudgetConfig,
        session_spent_cents: float = 0.0,
        day_spent_cents: float = 0.0,
        candidate_filter: Callable[[str, ModelMetadata], bool] | None = None,
    ) -> ModelSelection:
        """Select the best model in ``tier`` for this turn (D-23-X-seam-shape).

        Args:
            candidate_filter: Optional ``(canonical_id, ModelMetadata) -> bool``
                hard pre-gate applied to the resolved candidates *before*
                scoring. ``None`` (the default) preserves the Spec 23 behaviour
                byte-identically. Spec V5 D-V5-2 passes the voice first-token-
                latency gate here ("gate-then-score, layered on the
                IntelligentRouter"): when the gate empties the candidate set the
                selection degrades to the rule-based slot-0 model with reason
                ``"latency_gated"`` — never raises (a tier with no fast-enough
                model must still answer; the floor returns to the user).

        Raises:
            BudgetExceededError: The per-turn hard cap admits no candidate
                (criterion 7) — propagates, never degraded.
            IntelligentRoutingError: A metadata miss when the persona set
                ``fallback_to_rule_based_on_miss=False`` (opted out of graceful
                degradation).
        """
        raw = self._registry.candidate_models_for(tier)
        if len(raw) <= 1:
            # Single-backend / non-multi-model tier — nothing to choose.
            primary = canonical_model_id(*raw[0]) if raw else ""
            return ModelSelection(
                model=primary,
                fallback_engaged=True,
                fallback_reason="not_a_multi_model_tier",
            )

        primary_id = canonical_model_id(*raw[0])
        candidates, model_name_by_id = self._resolve_candidates(raw)
        if not candidates:
            return self._degrade_or_raise(
                primary_id,
                reason="metadata_miss",
                allow_fallback=intelligent.fallback_to_rule_based_on_miss,
            )
        if candidate_filter is not None:
            gated = [(cid, md) for cid, md in candidates if candidate_filter(cid, md)]
            if not gated:
                # The hard gate (e.g. Spec V5's voice TTFT gate) admitted nothing —
                # degrade to slot-0 so the turn still answers (never strand it).
                _LOG.warning("model selection gate emptied candidate set reason=latency_gated")
                return ModelSelection(
                    model=primary_id,
                    fallback_engaged=True,
                    fallback_reason="latency_gated",
                )
            candidates = gated

        weights = routing_budget.effective_weights(
            _weights_from_config(intelligent),
            session_spent_cents=session_spent_cents,
            max_cents_per_session=budget.max_cents_per_session,
            day_spent_cents=day_spent_cents,
            max_cents_per_day=budget.max_cents_per_day,
        )
        scored = score_models(
            candidates,
            context,
            weights,
            latency_override=self._latency_override(model_name_by_id),
        )
        if not scored:
            # Capability gate emptied the set — degrade (D-23-X-capability-filter-
            # layering: the tier was already deemed capable at Layer 1; do NOT raise).
            return ModelSelection(
                model=primary_id,
                fallback_engaged=True,
                fallback_reason="capability_filtered",
            )

        # Per-turn HARD cap — fail-loud, propagates (criterion 7).
        within = routing_budget.enforce_turn_cap(
            scored, max_cents_per_turn=budget.max_cents_per_turn, tier=tier
        )
        best = within[0]
        return ModelSelection(
            model=best.model_id,
            model_candidates=tuple(s.model_id for s in within),
            score_vector=best.axes,
            weights_used={
                "cost": weights.cost,
                "quality": weights.quality,
                "latency": weights.latency,
            },
            fallback_engaged=False,
            fallback_reason=None,
        )

    def metadata_for(self, canonical_id: str) -> ModelMetadata | None:
        """Resolve metadata for ONE id via the same chain ``select_model`` uses (Spec M1 gate).

        The M1 preferred-model capability gate calls this to read ``tools_supported`` for
        the persona's chosen model. Best-effort by contract: any resolver failure ⇒ ``None``
        ⇒ the gate fails OPEN (the runtime tier-chain fallback still protects the turn).
        """
        try:
            return self._resolver.resolve(canonical_id)
        except Exception:  # noqa: BLE001 — gate is best-effort; None ⇒ fail-open
            return None

    # ------------------------------------------------------------------ #

    def _resolve_candidates(
        self, raw: tuple[tuple[str, str], ...]
    ) -> tuple[list[Candidate], dict[str, str]]:
        """Resolve metadata for each ``(provider, model)``; drop misses.

        Returns the ``(canonical_id, ModelMetadata)`` pairs that resolved plus a
        ``canonical_id → model_name`` map (the latency tracker keys on the bare
        model name the backend reports).
        """
        candidates: list[Candidate] = []
        model_name_by_id: dict[str, str] = {}
        for provider, model in raw:
            cid = canonical_model_id(provider, model)
            md: ModelMetadata | None = self._resolver.resolve(cid)
            if md is None:
                continue
            candidates.append((cid, md))
            model_name_by_id[cid] = model
        return candidates, model_name_by_id

    def _latency_override(
        self, model_name_by_id: dict[str, str]
    ) -> Callable[[str], float | None] | None:
        """Build a ``canonical_id → live latency ms | None`` lookup (D-23-6).

        Returns the EWMA latency for a model that has ≥ N samples; ``None``
        (defer to static ``latency_p50_ms``) otherwise or when no tracker is set.
        """
        tracker = self._latency
        if tracker is None:
            return None

        def _lookup(canonical_id: str) -> float | None:
            model_name = model_name_by_id.get(canonical_id)
            if model_name is None:
                return None
            if tracker.sample_count(model_name) < _LATENCY_MIN_SAMPLES:
                return None
            return tracker.get(model_name)

        return _lookup

    def _degrade_or_raise(
        self, primary_id: str, *, reason: str, allow_fallback: bool
    ) -> ModelSelection:
        """Degrade to rule-based slot-0, or fail loud when the persona opted out."""
        if not allow_fallback:
            raise IntelligentRoutingError(
                "intelligent routing could not resolve model metadata and "
                "fallback_to_rule_based_on_miss is disabled",
                context={"reason": reason, "primary": primary_id},
            )
        _LOG.warning(
            "intelligent routing degraded to rule-based slot-0 reason={reason}",
            reason=reason,
        )
        return ModelSelection(model=primary_id, fallback_engaged=True, fallback_reason=reason)


def _weights_from_config(intelligent: IntelligentRoutingConfig) -> ProfileWeights:
    """Translate the persona's schema weights into the runtime scorer weights."""
    w = intelligent.weights
    return ProfileWeights(cost=w.cost, quality=w.quality, latency=w.latency)
