"""Composite scoring — additive relevance + recency + importance (Spec K9, T4; K9-D-5).

The final ranking signal, replacing K8's interim ``sim · max(R, floor)`` (which K8-D-4 says
"dies here"). Three **absolute ``[0, 1]``** components combined **additively** with
configurable weights (relevance-primary by default — a refinement of K9-D-5's min-max/
equal-weight prior, see :func:`apply_composite_score`):

- **relevance** — the reranked score (``candidate.relevance``, which the reranker overwrites
  with its absolute cross-encoder score; pre-rerank it is the dense cosine reading, and a
  candidate with no reading falls to the set floor);
- **recency** — episodic uses the landed strength-aware ``retention()`` (MemoryBank
  ``R = exp(−Δt/(τ₀·strength))``); graph nodes, which carry no strength counter, use a plain
  exp decay over their most-recent provenance time;
- **importance** — episodic's write-time scalar (``metadata['importance']``); graph nodes
  carry no write-time scalar, so their importance is 0 (relevance + recency carry them).

**Additive, never multiplicative** (K9-D-5): a zero component down-weights but never
annihilates, so an old high-relevance memory still surfaces and recency only breaks ties.
**Vetoes live in the abstention gate (T6), not here.** Pure and wall-clock-free — ``now`` is
injected (the K7 no-wallclock discipline).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from persona.stores.lifecycle import retention

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from persona.recall.config import RecallSettings
    from persona.recall.models import RecallCandidate
    from persona.stores.lifecycle import EpisodicSettings

__all__ = ["apply_composite_score", "composite_components"]


def _recency(
    candidate: RecallCandidate,
    *,
    now: datetime,
    settings: RecallSettings,
    episodic_settings: EpisodicSettings,
) -> float:
    """Recency in ``(0, 1]`` — strength-aware for episodic, plain decay for graph."""
    if candidate.chunk is not None:
        return retention(candidate.chunk, now=now, settings=episodic_settings)
    node = candidate.node
    assert node is not None  # noqa: S101 — a candidate is a chunk or a node
    learned = max((p.written_at for p in node.provenance), default=node.created_at)
    elapsed_h = max(0.0, (now - learned).total_seconds() / 3600.0)
    return math.exp(-elapsed_h / settings.recency_tau_hours)


def _importance(candidate: RecallCandidate) -> float:
    """Write-time importance in ``[0, 1]`` — episodic's scalar; 0 for graph nodes."""
    if candidate.chunk is None:
        return 0.0
    raw = candidate.chunk.metadata.get("importance")
    if raw is None:
        return 0.0
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        return 0.0


def composite_components(
    candidates: Sequence[RecallCandidate],
    *,
    now: datetime,
    settings: RecallSettings,
    episodic_settings: EpisodicSettings,
) -> tuple[list[float], list[float], list[float]]:
    """The three raw (un-normalised) component vectors, index-aligned to ``candidates``.

    Relevance uses ``candidate.relevance`` (the dense reading); a candidate with no reading
    contributes the set's minimum relevance so a sparse-only hit is not spuriously boosted.
    Exposed for the eval harness; :func:`apply_composite_score` consumes it.
    """
    rel_readings = [c.relevance for c in candidates if c.relevance is not None]
    rel_floor = min(rel_readings) if rel_readings else 0.0
    relevance = [c.relevance if c.relevance is not None else rel_floor for c in candidates]
    recency = [
        _recency(c, now=now, settings=settings, episodic_settings=episodic_settings)
        for c in candidates
    ]
    importance = [_importance(c) for c in candidates]
    return relevance, recency, importance


def apply_composite_score(
    candidates: Sequence[RecallCandidate],
    *,
    now: datetime,
    settings: RecallSettings,
    episodic_settings: EpisodicSettings,
) -> list[RecallCandidate]:
    """Score, re-rank, and return the candidates by the composite (K9-D-5, refined at T4).

    Each component is an **absolute ``[0, 1]`` reading** (relevance = the reranked score;
    recency = retention/decay; importance = the write-time scalar), combined additively with
    the configured weights, written to ``composite_score``, and the set re-sorted descending
    (ties broken by the fused ``rank`` for determinism) with ``rank`` renumbered.

    **Refinement of K9-D-5 (surfaced at the T4 gate):** components are **not min-max-
    normalised**, and the default weights make **relevance primary** (1.0) with recency and
    importance as smaller tie-breakers (~0.35). The Generative-Agents 1/1/1-with-min-max prior
    *violates* K9-D-5's own invariant ("an old high-relevance memory still surfaces; recency
    only breaks ties"): min-max destroys absolute magnitude, so a maximally-relevant-but-old
    memory ties a fresh-but-irrelevant one and recency can override a large relevance gap.
    Absolute components + relevance-primary weights make recency/importance genuine
    tie-breakers among comparably-relevant candidates, which is the invariant. 1/1/1 (and
    any weighting) stays available via config; P8 calibrates.

    Args:
        candidates: The reranked pool to score.
        now: The turn's reference time (injected — pure, no clock read).
        settings: The recall tunables (component weights + graph recency τ).
        episodic_settings: The K8 lifecycle settings (the strength-aware retention τ₀).

    Returns:
        The candidates with ``composite_score`` set and re-sorted best-first.
    """
    if not candidates:
        return []
    relevance, recency, importance = composite_components(
        candidates, now=now, settings=settings, episodic_settings=episodic_settings
    )
    scored = [
        c.model_copy(
            update={
                "composite_score": (
                    settings.weight_relevance * relevance[i]
                    + settings.weight_recency * recency[i]
                    + settings.weight_importance * importance[i]
                )
            }
        )
        for i, c in enumerate(candidates)
    ]
    scored.sort(key=lambda c: (-(c.composite_score or 0.0), c.rank, c.key))
    return [c.model_copy(update={"rank": rank}) for rank, c in enumerate(scored, start=1)]
