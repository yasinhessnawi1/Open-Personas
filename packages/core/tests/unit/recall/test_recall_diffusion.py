"""Graph diffusion — the gated, low-weight tiebreaker (Spec K9, T5; K9-D-8).

Pins: default-off on single-hop (structural discipline), the deterministic multi-hop rule
stack, the low-weight tiebreak that only nudges existing neighbours (never surfaces new
nodes), and graceful degradation — a false negative or a provider failure degrades quality,
never availability (never a crash).
"""

from __future__ import annotations

from persona.recall.config import RecallSettings
from persona.recall.diffusion import apply_diffusion, is_multi_hop
from persona.recall.models import RecallCandidate, RecallSource

from tests.unit.recall._fixtures import node

SETTINGS = RecallSettings()


def _g(key: str, *, relevance: float, composite: float, rank: int) -> RecallCandidate:
    return RecallCandidate(
        source=RecallSource.GRAPH,
        key=key,
        node=node(key),
        relevance=relevance,
        composite_score=composite,
        rank=rank,
    )


class _FakeNeighbours:
    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._mapping = mapping

    def neighbours(self, node_id: str) -> list[str]:
        return self._mapping.get(node_id, [])


class _BoomNeighbours:
    def neighbours(self, node_id: str) -> list[str]:  # noqa: ARG002
        raise RuntimeError("graph store unavailable")


# ---- detection ------------------------------------------------------------


def test_strong_single_hop_query_is_not_multi_hop() -> None:
    cands = [_g("a", relevance=0.9, composite=0.9, rank=1)]  # high confidence
    assert not is_multi_hop("what is my address", cands, settings=SETTINGS)


def test_low_confidence_triggers_multi_hop() -> None:
    cands = [_g("a", relevance=0.2, composite=0.2, rank=1)]  # below the floor
    assert is_multi_hop("my address", cands, settings=SETTINGS)


def test_multiple_entities_trigger_multi_hop() -> None:
    cands = [_g("a", relevance=0.9, composite=0.9, rank=1)]  # high confidence, but a bridge query
    assert is_multi_hop("who introduced Alice to Bob in Berlin", cands, settings=SETTINGS)


# ---- application ----------------------------------------------------------


def test_single_hop_query_leaves_the_pool_untouched() -> None:
    cands = [
        _g("a", relevance=0.9, composite=0.9, rank=1),
        _g("b", relevance=0.85, composite=0.85, rank=2),
    ]
    provider = _FakeNeighbours({"a": ["b"]})  # would boost b, but the gate is OFF
    out = apply_diffusion(cands, query="my address", provider=provider, settings=SETTINGS)
    assert [c.key for c in out] == ["a", "b"]
    assert all(c.diffusion_score is None for c in out)


def test_multi_hop_query_nudges_a_neighbour_of_a_strong_seed() -> None:
    # b starts below c; b is a neighbour of the top seed a → the 0.1 tiebreak lifts b over c.
    cands = [
        _g("a", relevance=0.2, composite=0.50, rank=1),
        _g("c", relevance=0.2, composite=0.45, rank=2),
        _g("b", relevance=0.2, composite=0.40, rank=3),
    ]
    provider = _FakeNeighbours({"a": ["b"]})
    out = apply_diffusion(cands, query="my thing", provider=provider, settings=SETTINGS)
    assert [c.key for c in out] == ["a", "b", "c"]  # b (0.40+0.1=0.50) now beats c (0.45)
    b = next(c for c in out if c.key == "b")
    assert b.diffusion_score == SETTINGS.diffusion_weight


def test_diffusion_never_surfaces_a_new_node() -> None:
    cands = [_g("a", relevance=0.2, composite=0.5, rank=1)]
    provider = _FakeNeighbours({"a": ["not_in_pool"]})  # neighbour absent from the pool
    out = apply_diffusion(cands, query="my thing", provider=provider, settings=SETTINGS)
    assert {c.key for c in out} == {"a"}  # a tiebreak, not a retriever


def test_provider_failure_degrades_to_the_untouched_pool() -> None:
    cands = [
        _g("a", relevance=0.2, composite=0.5, rank=1),
        _g("b", relevance=0.2, composite=0.4, rank=2),
    ]
    out = apply_diffusion(cands, query="my thing", provider=_BoomNeighbours(), settings=SETTINGS)
    assert [c.key for c in out] == ["a", "b"]  # graceful degradation — quality, not availability


def test_empty_pool_is_returned_empty() -> None:
    assert apply_diffusion([], query="q", provider=_FakeNeighbours({}), settings=SETTINGS) == []
