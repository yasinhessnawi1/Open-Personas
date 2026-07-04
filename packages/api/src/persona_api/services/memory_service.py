"""Memory (knowledge-graph UI) read projections over the K0 graph store (Spec K5).

Pure projection: shapes the K0 graph types (:class:`ConceptNode`, :class:`TypedLink`,
:class:`NodeProvenance`) into the Memory API response models. **No graph logic lives
here** — the store owns structure, retrieval, and policy (criterion 12). The owner is
always the authenticated caller (RLS-scoped); every function is a read (CQS — no writes).

Windowing (K5-D-2): the seed (no focus) or a one-hop focus neighbourhood — never the
whole graph. The constants below are the provisional sizes (B1-tuned later; config in a
follow-up — YAGNI for the read batch).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from persona.graph.config import GraphSettings
from persona.graph.retrieval import HybridRetriever

from persona_api.schemas.responses import (
    MemoryEvolutionEntry,
    MemoryLinkEdge,
    MemoryLinkView,
    MemoryNodeDetail,
    MemoryNodeSummary,
    MemoryProvenanceView,
    MemorySearchResponse,
    MemorySearchResult,
    MemoryWindowResponse,
)

if TYPE_CHECKING:
    from persona.graph.models import ConceptNode, TypedLink
    from persona.graph.protocol import GraphStore

# Provisional window sizes (K5-D-2 — B1-tuned; config later).
_SEED_LIMIT = 200
_NEIGHBOR_LIMIT = 60


def _summary(node: ConceptNode, *, degree: int = 0) -> MemoryNodeSummary:
    """Project a node to its canvas summary (no content/provenance — that's detail)."""
    return MemoryNodeSummary(
        id=node.id,
        kind=str(node.node_kind),
        label=node.concept_name,
        wellbeing_category=node.wellbeing_category,
        degree=degree,
    )


def build_window(
    store: GraphStore,
    owner_id: str,
    *,
    focus_id: str | None = None,
    seed_limit: int = _SEED_LIMIT,
    neighbor_limit: int = _NEIGHBOR_LIMIT,
) -> MemoryWindowResponse:
    """A windowed slice of the owner's graph — the seed, or a focus neighbourhood.

    No focus → the seed window (most-connected, most-recent; K5-D-8). A focus →
    that node plus its one-hop neighbours. ``total_nodes`` is the owner's full tally
    (the header), but ``nodes``/``links`` are only the loaded window (K5-D-2).
    """
    total = store.count_nodes(owner_id)
    if focus_id is None:
        nodes = store.seed_nodes(owner_id, limit=seed_limit)
        return _assemble_window(store, owner_id, nodes, focus_id=None, is_seed=True, total=total)

    focus = store.get_node(owner_id, focus_id)
    if focus is None:
        return MemoryWindowResponse(
            focus_id=focus_id, is_seed=False, total_nodes=total, nodes=[], links=[]
        )
    nodes = [focus]
    seen = {focus.id}
    # The focus's own edges (incl. on-the-fly ENTITY links) come from ``neighbors``;
    # neighbour↔neighbour stored edges come from ``edges_among`` below.
    focus_edges = store.neighbors(owner_id, focus_id, limit=neighbor_limit)
    for _edge, neighbor in focus_edges:
        if neighbor.id not in seen:
            seen.add(neighbor.id)
            nodes.append(neighbor)
    return _assemble_window(
        store,
        owner_id,
        nodes,
        focus_id=focus_id,
        is_seed=False,
        total=total,
        extra_edges=[edge for edge, _node in focus_edges],
    )


def _assemble_window(
    store: GraphStore,
    owner_id: str,
    nodes: list[ConceptNode],
    *,
    focus_id: str | None,
    is_seed: bool,
    total: int,
    extra_edges: list[TypedLink] | None = None,
) -> MemoryWindowResponse:
    node_ids = [n.id for n in nodes]
    edges = list(store.edges_among(owner_id, node_ids))
    if extra_edges:
        edges.extend(extra_edges)
    degree: dict[str, int] = dict.fromkeys(node_ids, 0)
    seen_edges: set[str] = set()
    link_views: list[MemoryLinkEdge] = []
    for edge in edges:
        if edge.id in seen_edges:
            continue
        seen_edges.add(edge.id)
        degree[edge.src_node_id] = degree.get(edge.src_node_id, 0) + 1
        degree[edge.dst_node_id] = degree.get(edge.dst_node_id, 0) + 1
        link_views.append(
            MemoryLinkEdge(
                src_node_id=edge.src_node_id,
                dst_node_id=edge.dst_node_id,
                link_type=str(edge.link_type),
                weight=edge.weight,
            )
        )
    node_views = [_summary(n, degree=degree.get(n.id, 0)) for n in nodes]
    return MemoryWindowResponse(
        focus_id=focus_id,
        is_seed=is_seed,
        total_nodes=total,
        nodes=node_views,
        links=link_views,
    )


def node_detail(
    store: GraphStore,
    owner_id: str,
    node_id: str,
    *,
    neighbor_limit: int = _NEIGHBOR_LIMIT,
) -> MemoryNodeDetail | None:
    """The node's full detail — content, provenance-as-story, evolution, typed links.

    Returns ``None`` when the node is not the owner's (the route 404s). The evolution
    is the node's full accumulation trail (D-K0-4); ``origin`` is its first
    contribution (where it came from). Typed links come from ``neighbors`` so the four
    relationships — including on-the-fly ENTITY threads — are all traversable.
    """
    node = store.get_node(owner_id, node_id)
    if node is None:
        return None
    trail = node.provenance  # at least one entry (ConceptNode invariant)
    origin = trail[0]
    links: list[MemoryLinkView] = []
    for edge, neighbor in store.neighbors(owner_id, node_id, limit=neighbor_limit):
        direction: Literal["out", "in"] = "out" if edge.src_node_id == node_id else "in"
        links.append(
            MemoryLinkView(
                link_type=str(edge.link_type),
                weight=edge.weight,
                direction=direction,
                neighbor=_summary(neighbor),
            )
        )
    return MemoryNodeDetail(
        id=node.id,
        kind=str(node.node_kind),
        label=node.concept_name,
        content=node.content,
        wellbeing_category=node.wellbeing_category,
        created_at=node.created_at,
        origin=MemoryProvenanceView(
            source=str(origin.source),
            persona_id=origin.persona_id,
            interaction_id=origin.interaction_id,
            written_at=origin.written_at,
            reason=origin.reason,
            grounding=origin.grounding,
        ),
        evolution=[
            MemoryEvolutionEntry(
                source=str(p.source),
                written_at=p.written_at,
                reason=p.reason,
                superseded_content=p.superseded_content,
            )
            for p in trail
        ],
        links=links,
    )


def search(
    store: GraphStore,
    owner_id: str,
    query: str,
    *,
    settings: GraphSettings | None = None,
    top_k: int | None = None,
) -> MemorySearchResponse:
    """Search-to-navigate over the owner's graph — K1 hybrid (criterion 5).

    Exact-term and paraphrase both surface; ``dense_rank``/``sparse_rank`` are passed
    through so the UI can show *why* a node matched. **No K4 subtraction** here
    (``allowlist=None``): this is the user's own view and they see + control their
    flagged nodes (criterion 8), unlike persona retrieval which gates them (D-K1-7).
    """
    retriever = HybridRetriever(store=store, settings=settings or GraphSettings())
    results = retriever.retrieve(owner_id, query, allowlist=None, top_k=top_k)
    return MemorySearchResponse(
        query=query,
        results=[
            MemorySearchResult(
                node_id=r.node.id,
                label=r.node.concept_name,
                kind=str(r.node.node_kind),
                score=r.score,
                dense_rank=r.dense_rank,
                sparse_rank=r.sparse_rank,
            )
            for r in results
        ],
    )


def correct(
    store: GraphStore,
    owner_id: str,
    node_id: str,
    new_content: str,
    *,
    interaction_id: str | None = None,
) -> MemoryNodeDetail:
    """Apply a user's correction, then return the node's fresh detail (criterion 6).

    The write (re-embed, re-index, semantic links re-evaluated, provenance → user-edited)
    is the store's :meth:`correct_node`; this re-queries the corrected node so the caller
    (the ``PATCH`` route) returns the updated detail. Propagates
    ``GraphNodeNotFoundError`` (the route maps it to 404) when the node is not the owner's.
    """
    store.correct_node(owner_id, node_id, new_content, interaction_id=interaction_id)
    detail = node_detail(store, owner_id, node_id)
    if detail is None:  # pragma: no cover — correct_node already raised if absent
        from persona.graph.errors import GraphNodeNotFoundError

        raise GraphNodeNotFoundError(
            "memory vanished after correction", context={"node_id": node_id, "owner_id": owner_id}
        )
    return detail


def delete(store: GraphStore, owner_id: str, node_id: str) -> bool:
    """Delete a node — gone from Postgres AND the index in the same path (criterion 7).

    Returns ``True`` if the node existed (and is now removed everywhere — including from
    every persona's retrieval, via K0's same-path sync), ``False`` if it was not the
    owner's (the route 404s). The trust-critical action: a deletion that doesn't truly
    delete is the worst breach in the product (K5 §7). CQS — returns confirmation only.
    """
    return store.delete_node(owner_id, node_id)


__all__ = ["build_window", "correct", "delete", "node_detail", "search"]
