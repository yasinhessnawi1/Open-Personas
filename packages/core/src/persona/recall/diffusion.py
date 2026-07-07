"""Graph diffusion — a gated, low-weight tiebreaker, never an engine (Spec K9, T5; K9-D-8).

Graph spreading (personalized-PageRank-style) helps **multi-hop/bridge** queries and is
**neutral-to-harmful on single-hop** (HippoRAG2: single-hop ~0; "Use Graph When It Needs"
2602.03578: unconditional GraphRAG −13.4% on single-hop NQ). So K9 uses it as a
**low-weight (~0.1) additive tiebreak, gated ON only for detected multi-hop queries** —
GAAMA 2603.27910's ``w_ppr=0.1`` is the near-identical published precedent. It **never
surfaces new nodes** (that would make it a retriever): it only nudges candidates already in
the pool that are graph-neighbours of the strong seeds — a tiebreak, by construction.

Two disciplines are asserted here, not hoped:
- **Detection is a deterministic rule stack, no turn-path model call** (K9-D-8): low one-hop
  confidence OR ≥ N distinct query entities. Off ⇒ the pool is returned untouched.
- **Graceful degradation** (the named test): a mis-detection or a provider failure
  **degrades quality, never availability** — a false negative just skips the tiebreak (the
  single-hop pool stands), and any neighbour-provider error is swallowed to the untouched
  pool. Diffusion can reorder; it can never crash or block the turn.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from persona.logging import get_logger
from persona.recall.models import RecallCandidate, RecallSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.recall.config import RecallSettings

__all__ = ["NeighbourProvider", "apply_diffusion", "is_multi_hop"]

_LOG = "recall.diffusion"

#: Proper-noun-ish token: a capitalised word (len>1) not counted at sentence start. A cheap,
#: deterministic entity proxy — the multi-hop signal is "≥2 distinct entities to bridge".
_CAP_TOKEN = re.compile(r"\b([A-Z][a-zA-Z]{1,})\b")
#: Relational connectives that signal a bridge ("who directed the film that…").
_BRIDGE_RE = re.compile(r"\b(who|whose|which|that|where)\b.*\b(the|a|an)\b|'s\b", re.IGNORECASE)


class NeighbourProvider(Protocol):
    """One-hop graph neighbours of a node (composition-bound over K7's ``neighbors``, T8/T9).

    Injected so diffusion is pure and testable; the composition binds the owner-scoped graph
    store. Returns neighbour node ids (bounded by the caller's per-seed cap).
    """

    def neighbours(self, node_id: str) -> Sequence[str]:
        """Return the one-hop neighbour node ids of ``node_id`` (bounded)."""
        ...


def _distinct_entities(query: str) -> int:
    """Count distinct capitalised entity tokens, ignoring the sentence-initial word."""
    tokens = _CAP_TOKEN.findall(query)
    if tokens and query.lstrip().startswith(tokens[0]):
        tokens = tokens[1:]  # the first word is capitalised by convention, not entity-hood
    return len({t.lower() for t in tokens})


def is_multi_hop(
    query: str, candidates: Sequence[RecallCandidate], *, settings: RecallSettings
) -> bool:
    """The deterministic multi-hop/bridge gate — the rule stack (K9-D-8, no model call).

    Multi-hop when either (a) **one-hop confidence is low** — the best relevance reading in
    the pool is below ``diffusion_confidence_floor`` (the single-hop set is uncertain, so a
    bridge may help), or (b) the query names **≥ ``diffusion_min_query_entities`` distinct
    entities** or carries a relational connective (a bridge query by surface form). Otherwise
    single-hop — diffusion stays OFF.
    """
    readings = [c.relevance for c in candidates if c.relevance is not None]
    top = max(readings) if readings else 0.0
    low_confidence = top < settings.diffusion_confidence_floor
    entities = _distinct_entities(query)
    bridge_form = entities >= settings.diffusion_min_query_entities or bool(
        _BRIDGE_RE.search(query)
    )
    return low_confidence or bridge_form


def apply_diffusion(
    candidates: Sequence[RecallCandidate],
    *,
    query: str,
    provider: NeighbourProvider,
    settings: RecallSettings,
) -> list[RecallCandidate]:
    """Apply the gated, low-weight diffusion tiebreak (K9-D-8) — or return the pool untouched.

    When the multi-hop gate is OFF, the pool is returned unchanged (default-off — the
    single-hop path). When ON, a small ``diffusion_weight`` is added to the composite score of
    any candidate that is a one-hop graph-neighbour of the top graph seeds — a bounded,
    deterministic, one-step personalized spread (``diffusion_max_seeds`` ×
    ``diffusion_per_seed_neighbours``). It **never adds new candidates**. On any provider
    failure the untouched pool is returned (graceful degradation — quality, not availability).

    Args:
        candidates: The composite-scored pool (``composite_score`` set).
        query: The turn's query (the multi-hop detector reads it).
        provider: The one-hop neighbour resolver over K7 (injected).
        settings: The recall tunables (``diffusion_weight`` + the gate/seed caps).

    Returns:
        The pool, re-scored + re-sorted if diffusion applied, else unchanged.
    """
    if not candidates or not is_multi_hop(query, candidates, settings=settings):
        return list(candidates)
    try:
        seeds = [c for c in candidates if c.source is RecallSource.GRAPH][
            : settings.diffusion_max_seeds
        ]
        boosted: set[str] = set()
        for seed in seeds:
            for neighbour_id in list(provider.neighbours(seed.key))[
                : settings.diffusion_per_seed_neighbours
            ]:
                boosted.add(neighbour_id)
    except Exception:  # noqa: BLE001 — graceful degradation: never crash the turn
        get_logger(_LOG).warning(
            "diffusion provider failed; single-hop order (n=%d)", len(candidates)
        )
        return list(candidates)

    if not boosted:
        return list(candidates)
    rescored = [
        c.model_copy(
            update={
                "diffusion_score": settings.diffusion_weight,
                "composite_score": (c.composite_score or 0.0) + settings.diffusion_weight,
            }
        )
        if c.key in boosted
        else c
        for c in candidates
    ]
    rescored.sort(key=lambda c: (-(c.composite_score or 0.0), c.rank, c.key))
    return [c.model_copy(update={"rank": rank}) for rank, c in enumerate(rescored, start=1)]
