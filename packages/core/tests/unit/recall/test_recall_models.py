"""RecallCandidate shape invariants (Spec K9, T1) — the one heterogeneous contract.

Pins: exactly-one-payload consistent with source (episodic→chunk, graph→node), the
key-must-equal-payload-id tamper check, per-leg provenance observability (a leg-rank of
None makes "surfaced by one leg only" visible), frozen immutability, and the rerank_text
accessor a candidate must always yield.
"""

from __future__ import annotations

import pytest
from persona.recall.models import LEG_SOURCE, RecallCandidate, RecallLeg, RecallSource
from pydantic import ValidationError

from tests.unit.recall._fixtures import chunk, node


def test_graph_candidate_carries_a_node_and_no_chunk() -> None:
    c = RecallCandidate(source=RecallSource.GRAPH, key="g1", node=node("g1"))
    assert c.node is not None
    assert c.chunk is None


def test_episodic_candidate_carries_a_chunk_and_no_node() -> None:
    c = RecallCandidate(source=RecallSource.EPISODIC_RAW, key="e1", chunk=chunk("e1"))
    assert c.chunk is not None
    assert c.node is None


def test_graph_source_rejects_a_chunk_payload() -> None:
    with pytest.raises(ValidationError):
        RecallCandidate(source=RecallSource.GRAPH, key="e1", chunk=chunk("e1"))


def test_episodic_source_rejects_a_node_payload() -> None:
    with pytest.raises(ValidationError):
        RecallCandidate(source=RecallSource.EPISODIC_RAW, key="g1", node=node("g1"))


def test_missing_payload_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RecallCandidate(source=RecallSource.EPISODIC_RAW, key="e1")


def test_two_payloads_are_rejected() -> None:
    with pytest.raises(ValidationError):
        RecallCandidate(source=RecallSource.GRAPH, key="g1", node=node("g1"), chunk=chunk("g1"))


def test_key_must_equal_the_payload_id() -> None:
    with pytest.raises(ValidationError):
        RecallCandidate(source=RecallSource.GRAPH, key="wrong", node=node("g1"))


def test_per_leg_ranks_default_to_none_for_observability() -> None:
    c = RecallCandidate(source=RecallSource.GRAPH, key="g1", node=node("g1"), graph_dense_rank=1)
    assert c.graph_dense_rank == 1
    assert c.graph_sparse_rank is None  # surfaced by the dense leg only — observable
    assert c.episodic_raw_rank is None


def test_candidate_is_frozen() -> None:
    c = RecallCandidate(source=RecallSource.EPISODIC_RAW, key="e1", chunk=chunk("e1"))
    with pytest.raises(ValidationError):
        c.rank = 5  # type: ignore[misc]


def test_rerank_text_from_a_node_is_name_then_content() -> None:
    c = RecallCandidate(
        source=RecallSource.GRAPH,
        key="g1",
        node=node("g1", name="Oslo", content="moved there in 2023"),
    )
    assert c.rerank_text == "Oslo moved there in 2023"


def test_rerank_text_from_a_chunk_is_its_text() -> None:
    c = RecallCandidate(source=RecallSource.EPISODIC_RAW, key="e1", chunk=chunk("e1", text="hello"))
    assert c.rerank_text == "hello"


def test_every_leg_maps_to_a_source() -> None:
    assert set(LEG_SOURCE) == set(RecallLeg)
    assert LEG_SOURCE[RecallLeg.GRAPH_TRAVERSAL] is RecallSource.GRAPH
    assert LEG_SOURCE[RecallLeg.EPISODIC_GIST] is RecallSource.EPISODIC_GIST
