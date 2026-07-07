"""Leg adapters — K1 reuse + injected store surfaces (Spec K9, T2).

Pins: graph_legs decomposes K1's HybridResult provenance back into dense/sparse/traversal
node legs (no re-query, no store fork), and gather_legs assembles from injected callables,
omitting absent (None) and empty structures so the fuse degrades to whatever answered.
"""

from __future__ import annotations

from persona.graph.fusion import HybridResult
from persona.graph.models import LinkType
from persona.recall.models import RecallLeg
from persona.recall.sources import gather_legs, graph_legs

from tests.unit.recall._fixtures import chunk, node


def _hybrid(
    node_id: str,
    *,
    rank: int,
    dense_rank: int | None = None,
    sparse_rank: int | None = None,
    via_traversal: bool = False,
) -> HybridResult:
    return HybridResult(
        node=node(node_id, distance=0.1 if dense_rank else None),
        score=1.0 / rank,
        rank=rank,
        dense_rank=dense_rank,
        sparse_rank=sparse_rank,
        via_traversal=via_traversal,
        traversal_link_type=LinkType.ENTITY if via_traversal else None,
    )


def test_graph_legs_splits_provenance_into_three_legs() -> None:
    results = [
        _hybrid("d", rank=1, dense_rank=1),
        _hybrid("ds", rank=2, dense_rank=2, sparse_rank=1),
        _hybrid("s", rank=3, sparse_rank=2),
        _hybrid("t", rank=4, via_traversal=True),
    ]
    legs = graph_legs(results)
    assert [n.id for n in legs[RecallLeg.GRAPH_DENSE]] == ["d", "ds"]
    assert [n.id for n in legs[RecallLeg.GRAPH_SPARSE]] == ["ds", "s"]
    assert [n.id for n in legs[RecallLeg.GRAPH_TRAVERSAL]] == ["t"]


def test_graph_legs_orders_each_leg_by_its_own_leg_rank() -> None:
    # Fused order (rank) differs from per-leg order; the leg must use the LEG rank.
    results = [
        _hybrid("a", rank=1, dense_rank=2),
        _hybrid("b", rank=2, dense_rank=1),
    ]
    legs = graph_legs(results)
    assert [n.id for n in legs[RecallLeg.GRAPH_DENSE]] == ["b", "a"]  # by dense_rank, not rank


def test_graph_legs_omits_empty_legs() -> None:
    legs = graph_legs([_hybrid("d", rank=1, dense_rank=1)])
    assert set(legs) == {RecallLeg.GRAPH_DENSE}


def test_gather_legs_assembles_all_three_structures() -> None:
    legs = gather_legs(
        query="q",
        top_k=5,
        episodic_query=lambda _q, _k: [chunk("e1", distance=0.1)],
        gist_query=lambda _q, _k: [chunk("gi", distance=0.1)],
        graph_retrieve=lambda _q: [_hybrid("g1", rank=1, dense_rank=1)],
    )
    assert set(legs) == {
        RecallLeg.EPISODIC_RAW,
        RecallLeg.EPISODIC_GIST,
        RecallLeg.GRAPH_DENSE,
    }


def test_gather_legs_omits_absent_and_empty_sources() -> None:
    legs = gather_legs(
        query="q",
        top_k=5,
        episodic_query=lambda _q, _k: [],  # empty → omitted
        graph_retrieve=None,  # absent → omitted
    )
    assert legs == {}


def test_gather_legs_with_no_sources_is_empty() -> None:
    assert gather_legs(query="q", top_k=5) == {}
