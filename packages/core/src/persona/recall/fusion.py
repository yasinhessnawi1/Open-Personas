"""Fuse-don't-route — N heterogeneous legs → one ranked candidate set (Spec K9, T2).

The architectural spine (K9-D-1): there is **no episodic-vs-graph router**. Every leg —
raw-episodic dense, gist dense, graph dense, graph BM25, graph one-hop — is retrieved
independently and **fused into one candidate set**, so a query whose answer lives
episodic-only, graph-only, or in both is recalled through the *same* path. A strong
similarity-gated baseline collapses 56% on the low-cosine subset while a fused set
degrades ~7% (SYNAPSE 2601.02744, Table 9 — the motivation is "fusion beats
similarity-gating," not "vectors collapse").

Fusion is **weighted RRF** (K9-D-2), carried forward from K1: rank-based (cross-leg score
scales are incomparable — a pgvector cosine vs a ``ts_rank`` vs a graph one-hop — so it
crosses *rank*, not score, and needs no normalization). Two things K1's fuse did not do:
it spans **structures** (chunks + gists + nodes), and it applies a **per-source quota** so
one dominant leg cannot crowd the graph or the gists out of the pool — recall balance,
because the downstream cross-encoder (K9-D-3) does the ordering. Convex/DBSF score fusion
is deliberately *not* here: it only earns its tuning cost on the un-reranked voice
fallback (K9-D-2), where no reranker rescues ordering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.recall.models import LEG_SOURCE, RecallCandidate, RecallLeg, RecallSource

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from persona.graph.models import ConceptNode
    from persona.recall.config import RecallSettings
    from persona.schema.chunks import PersonaChunk

__all__ = ["fuse"]

#: Leg → the ``RecallSettings`` attribute holding its RRF weight (K9-D-2).
_LEG_WEIGHT_ATTR: dict[RecallLeg, str] = {
    RecallLeg.EPISODIC_RAW: "weight_episodic_raw",
    RecallLeg.EPISODIC_GIST: "weight_episodic_gist",
    RecallLeg.GRAPH_DENSE: "weight_graph_dense",
    RecallLeg.GRAPH_SPARSE: "weight_graph_sparse",
    RecallLeg.GRAPH_TRAVERSAL: "weight_graph_traversal",
}

#: Leg → the ``RecallSettings`` attribute holding its per-source quota (K9-D-2).
_LEG_QUOTA_ATTR: dict[RecallLeg, str] = {
    RecallLeg.EPISODIC_RAW: "quota_episodic_raw",
    RecallLeg.EPISODIC_GIST: "quota_episodic_gist",
    RecallLeg.GRAPH_DENSE: "quota_graph_dense",
    RecallLeg.GRAPH_SPARSE: "quota_graph_sparse",
    RecallLeg.GRAPH_TRAVERSAL: "quota_graph_traversal",
}

#: Leg → the ``RecallCandidate`` field its 1-indexed rank populates (observability).
_LEG_RANK_FIELD: dict[RecallLeg, str] = {
    RecallLeg.EPISODIC_RAW: "episodic_raw_rank",
    RecallLeg.EPISODIC_GIST: "gist_rank",
    RecallLeg.GRAPH_DENSE: "graph_dense_rank",
    RecallLeg.GRAPH_SPARSE: "graph_sparse_rank",
    RecallLeg.GRAPH_TRAVERSAL: "graph_traversal_rank",
}


class _Accumulator:
    """Mutable per-key fusion state — collapsed into a frozen candidate at the end."""

    __slots__ = ("obj", "owner_id", "persona_id", "ranks", "score", "source")

    def __init__(self, source: RecallSource) -> None:
        self.source = source
        self.score = 0.0
        self.ranks: dict[RecallLeg, int] = {}
        self.obj: PersonaChunk | ConceptNode | None = None
        self.owner_id: str | None = None
        self.persona_id: str | None = None


def _obj_id(obj: PersonaChunk | ConceptNode) -> str:
    return obj.id


def _distance(obj: PersonaChunk | ConceptNode) -> float | None:
    # Both PersonaChunk and ConceptNode expose an optional query-time ``distance``.
    return obj.distance


def fuse(
    *,
    legs: Mapping[RecallLeg, Sequence[PersonaChunk | ConceptNode]],
    settings: RecallSettings,
    owner_id: str | None,
    persona_id: str | None,
    top_k: int,
) -> list[RecallCandidate]:
    """Fuse the retrieval legs into one ranked :class:`RecallCandidate` list (K9-D-1/2).

    Each leg is truncated to its per-source quota, its members enumerated at 1-indexed
    rank, and every candidate scored ``Σ_leg weight_leg · 1/(rrf_k + rank_leg)`` over the
    legs it appears in. A candidate in only one leg keeps that single contribution — it is
    never gated out by absence from another (the no-gating property). Ties break by
    ``key`` ascending for a deterministic, stable order.

    Structures never collide: episodic chunk ids and graph node ids are disjoint
    namespaces, so a key belongs to exactly one :class:`RecallSource`. The two scoping
    keys are stamped from the (single) resolved owner/persona for the turn, never crossed:
    graph candidates carry ``owner_id``; episodic candidates carry ``persona_id``.

    Args:
        legs: The retrieved legs, best-first per leg. Missing/empty legs are fine.
        settings: The fusion tunables (``rrf_k``, per-leg weights + quotas).
        owner_id: The turn's graph owner scope, stamped onto graph candidates.
        persona_id: The turn's episodic persona scope, stamped onto episodic candidates.
        top_k: Maximum fused candidates to return (the pool size handed to the reranker).

    Returns:
        Up to ``top_k`` :class:`RecallCandidate`, fused-score-descending, 1-indexed ranks.
    """
    acc: dict[str, _Accumulator] = {}
    for leg, items in legs.items():
        source = LEG_SOURCE[leg]
        weight = float(getattr(settings, _LEG_WEIGHT_ATTR[leg]))
        quota = int(getattr(settings, _LEG_QUOTA_ATTR[leg]))
        for rank, obj in enumerate(items[:quota], start=1):
            key = _obj_id(obj)
            entry = acc.get(key)
            if entry is None:
                entry = _Accumulator(source)
                acc[key] = entry
            entry.score += weight * (1.0 / (settings.rrf_k + rank))
            entry.ranks[leg] = rank
            # Prefer the object instance that carries a dense ``distance`` (the relevance
            # reading); otherwise keep the first seen.
            if entry.obj is None or (_distance(entry.obj) is None and _distance(obj) is not None):
                entry.obj = obj
            if source is RecallSource.GRAPH:
                entry.owner_id = owner_id
            else:
                entry.persona_id = persona_id

    ordered = sorted(acc.items(), key=lambda kv: (-kv[1].score, kv[0]))
    return [
        _to_candidate(key, entry, rank)
        for rank, (key, entry) in enumerate(ordered[:top_k], start=1)
    ]


def _to_candidate(key: str, entry: _Accumulator, rank: int) -> RecallCandidate:
    assert entry.obj is not None  # noqa: S101 — an accumulator exists only after a hit
    dist = _distance(entry.obj)
    relevance = None if dist is None else 1.0 - dist
    is_graph = entry.source is RecallSource.GRAPH
    candidate = RecallCandidate(
        source=entry.source,
        key=key,
        node=entry.obj if is_graph else None,  # type: ignore[arg-type]
        chunk=None if is_graph else entry.obj,  # type: ignore[arg-type]
        owner_id=entry.owner_id,
        persona_id=entry.persona_id,
        fused_score=entry.score,
        rank=rank,
        relevance=relevance,
    )
    # The per-leg ranks (only the ``*_rank`` int fields) applied via update so the mixed
    # candidate field types (int-rank fields alongside the bool provenance flags) type-check.
    rank_fields = {_LEG_RANK_FIELD[leg]: r for leg, r in entry.ranks.items()}
    return candidate.model_copy(update=rank_fields) if rank_fields else candidate
