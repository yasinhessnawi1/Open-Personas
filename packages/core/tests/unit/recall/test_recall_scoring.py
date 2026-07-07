"""Composite scoring — additive relevance + recency + importance (Spec K9, T4; K9-D-5).

Pins: the additive triad re-ranks by all three signals, min-max normalisation, recency uses
the strength-aware retention for episodic and plain decay for graph, importance is episodic's
write-time scalar (0 for graph), a zero component down-weights but never annihilates (an old
high-relevance memory still surfaces), and equal-weight is the tunable prior.
"""

from __future__ import annotations

from persona.recall.config import RecallSettings
from persona.recall.models import RecallCandidate, RecallSource
from persona.recall.scoring import apply_composite_score, composite_components
from persona.stores.lifecycle import EpisodicSettings

from tests.unit.recall._fixtures import NOW, chunk, node

SETTINGS = RecallSettings()
EPISODIC = EpisodicSettings()


def _ep(
    key: str,
    *,
    distance: float,
    importance: float | None = None,
    age_hours: float = 0.0,
    rank: int = 1,
) -> RecallCandidate:
    return RecallCandidate(
        source=RecallSource.EPISODIC_RAW,
        key=key,
        chunk=chunk(key, distance=distance, importance=importance, age_hours=age_hours),
        relevance=1.0 - distance,
        rank=rank,
    )


def _score(cands: list[RecallCandidate]) -> list[RecallCandidate]:
    return apply_composite_score(cands, now=NOW, settings=SETTINGS, episodic_settings=EPISODIC)


def test_higher_relevance_ranks_higher_all_else_equal() -> None:
    out = _score([_ep("a", distance=0.5, rank=2), _ep("b", distance=0.1, rank=1)])
    assert [c.key for c in out] == ["b", "a"]
    assert out[0].composite_score is not None


def test_importance_lifts_an_otherwise_equal_candidate() -> None:
    # Equal relevance + recency; b is pinned-important → b outranks a.
    out = _score([_ep("a", distance=0.3, importance=0.0), _ep("b", distance=0.3, importance=1.0)])
    assert [c.key for c in out] == ["b", "a"]


def test_recency_lifts_a_fresher_candidate_all_else_equal() -> None:
    # Equal relevance + importance; a is old, b is fresh → b outranks a.
    out = _score(
        [
            _ep("a", distance=0.3, age_hours=5000.0),
            _ep("b", distance=0.3, age_hours=0.0),
        ]
    )
    assert [c.key for c in out] == ["b", "a"]


def test_recency_only_breaks_ties_it_never_kills_a_relevant_memory() -> None:
    # An OLD but highly-relevant memory must still beat a FRESH weak one (additive, not
    # multiplicative — the "decay ranks memory dead" failure is avoided).
    out = _score(
        [
            _ep("old_relevant", distance=0.02, age_hours=9000.0),
            _ep("fresh_weak", distance=0.9, age_hours=0.0),
        ]
    )
    assert out[0].key == "old_relevant"


def test_graph_node_importance_is_zero_but_it_still_scores() -> None:
    graph = RecallCandidate(
        source=RecallSource.GRAPH, key="g1", node=node("g1", distance=0.1), relevance=0.9, rank=1
    )
    ep = _ep("e1", distance=0.8, rank=2)
    out = _score([graph, ep])
    assert out[0].key == "g1"  # graph carried by relevance+recency, importance 0


def test_components_expose_the_three_raw_vectors() -> None:
    cands = [_ep("a", distance=0.2, importance=0.5)]
    rel, rec, imp = composite_components(
        cands, now=NOW, settings=SETTINGS, episodic_settings=EPISODIC
    )
    assert rel == [0.8]
    assert 0.0 < rec[0] <= 1.0
    assert imp == [0.5]


def test_empty_input_scores_to_empty() -> None:
    assert _score([]) == []


def test_weights_are_tunable() -> None:
    # Zero the recency + importance weights → pure relevance order.
    s = RecallSettings(weight_recency=0.0, weight_importance=0.0)
    out = apply_composite_score(
        [_ep("a", distance=0.3, age_hours=0.0), _ep("b", distance=0.1, age_hours=9000.0)],
        now=NOW,
        settings=s,
        episodic_settings=EPISODIC,
    )
    assert [c.key for c in out] == ["b", "a"]  # b more relevant despite being old
