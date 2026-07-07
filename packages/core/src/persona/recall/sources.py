"""Leg adapters — turn the existing store surfaces into K9's fusion legs (Spec K9, T2).

*Fuse-don't-route* (K9-D-1) needs each structure's best candidates as a ranked leg. This
module adapts the **landed** read surfaces into that shape **without forking any store**:

- **Episodic-raw** and **gist** legs are the K8 dense queries (raw chunks, and gist rows
  as peers — RAPTOR's collapsed multi-resolution pool, K8 handover §3), injected as
  ``query -> chunks`` callables so the composition binds the owner/persona once.
- **Graph** legs reuse **K1's ``HybridRetriever`` wholesale**: it already executes the
  graph's dense + BM25 + one-hop legs, and its ``HybridResult`` carries the per-leg
  provenance (``dense_rank`` / ``sparse_rank`` / ``via_traversal``). We **decompose that
  provenance back into three ranked node legs** (:func:`graph_legs`) so K9's one RRF fuses
  them uniformly with the episodic legs — K1 is the graph *leg-executor*, K9 owns the
  unified fusion. No re-query, no re-rank, no store fork.

Everything here is pure over injected callables/outputs (duck-typed, the D-05-4
discipline), so the real stores wire in at the composition root (T8) and fakes substitute
in tests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.recall.models import RecallLeg

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.graph.fusion import HybridResult
    from persona.graph.models import ConceptNode
    from persona.schema.chunks import PersonaChunk

__all__ = ["gather_legs", "graph_legs"]


def graph_legs(
    results: Sequence[HybridResult],
) -> dict[RecallLeg, list[ConceptNode]]:
    """Decompose K1's fused graph results back into three ranked node legs (K9-D-1).

    K1 already ran the graph's dense, sparse, and one-hop legs and recorded each node's
    per-leg provenance on its :class:`~persona.graph.fusion.HybridResult`. We recover the
    three legs so K9's unified RRF re-fuses them alongside the episodic legs (rather than
    treating K1's internal fused score as one opaque graph contribution):

    - **dense** — results with a ``dense_rank``, ordered by it;
    - **sparse** — results with a ``sparse_rank``, ordered by it;
    - **traversal** — results that entered ``via_traversal``, in K1's appended order.

    A node present in both retrieval legs appears in both lists (K9's fuse re-unions it by
    id — the no-gating property). Returns only non-empty legs.

    Args:
        results: The K1 hybrid retrieval for this turn (fused-rank order).

    Returns:
        A leg → ranked ``ConceptNode`` list mapping, empty legs omitted.
    """
    dense = sorted(
        (r for r in results if r.dense_rank is not None),
        key=lambda r: r.dense_rank,  # type: ignore[arg-type,return-value]
    )
    sparse = sorted(
        (r for r in results if r.sparse_rank is not None),
        key=lambda r: r.sparse_rank,  # type: ignore[arg-type,return-value]
    )
    traversal = [r for r in results if r.via_traversal]
    legs: dict[RecallLeg, list[ConceptNode]] = {}
    if dense:
        legs[RecallLeg.GRAPH_DENSE] = [r.node for r in dense]
    if sparse:
        legs[RecallLeg.GRAPH_SPARSE] = [r.node for r in sparse]
    if traversal:
        legs[RecallLeg.GRAPH_TRAVERSAL] = [r.node for r in traversal]
    return legs


def gather_legs(
    *,
    query: str,
    top_k: int,
    episodic_query: Callable[[str, int], Sequence[PersonaChunk]] | None = None,
    gist_query: Callable[[str, int], Sequence[PersonaChunk]] | None = None,
    graph_retrieve: Callable[[str], Sequence[HybridResult]] | None = None,
) -> dict[RecallLeg, list[PersonaChunk] | list[ConceptNode]]:
    """Assemble all fusion legs for one turn from the injected retrieval surfaces (K9-D-1).

    Each source is optional (``None`` ⇒ that structure contributes no leg — e.g. a persona
    with no graph, or a store with no gists yet), so the fuse degrades gracefully to
    whichever structures answered. The callables are owner/persona-bound by the
    composition; this function stays pure over them.

    Args:
        query: This turn's query text.
        top_k: Per-leg over-fetch (the fuse then applies per-source quotas).
        episodic_query: ``(query, k) -> raw chunks`` (``EpisodicStore.query`` bound).
        gist_query: ``(query, k) -> gist chunks`` (a dense query over ``episodic_gist``).
        graph_retrieve: ``query -> [HybridResult]`` (``HybridRetriever.retrieve`` bound).

    Returns:
        The leg → ranked candidates mapping to hand :func:`~persona.recall.fusion.fuse`.
    """
    legs: dict[RecallLeg, list[PersonaChunk] | list[ConceptNode]] = {}
    if episodic_query is not None:
        raw = list(episodic_query(query, top_k))
        if raw:
            legs[RecallLeg.EPISODIC_RAW] = raw
    if gist_query is not None:
        gists = list(gist_query(query, top_k))
        if gists:
            legs[RecallLeg.EPISODIC_GIST] = gists
    if graph_retrieve is not None:
        legs.update(graph_legs(graph_retrieve(query)))
    return legs
