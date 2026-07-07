"""The abstention gate composed with K4 (Spec K9, T6; K9-D-9 — the load-bearing seam).

Pins: the deterministic confidence predicate (floor + margin/mass, question-type-aware), and
the K4 composition — safety subtracts FIRST, abstention evaluates the POST-POLICY remainder;
confidence is measured on the set that could actually surface; one honest seam with two
internal reason codes (LOW_CONFIDENCE vs WITHHELD_BY_POLICY), the reason never surfaced.
"""

from __future__ import annotations

from persona.recall.config import RecallSettings
from persona.recall.contiguity import QuestionType
from persona.recall.gate import AbstentionReason, compose_gate, passes_confidence
from persona.recall.models import RecallCandidate, RecallSource

from tests.unit.recall._fixtures import chunk, node

SETTINGS = RecallSettings()


def _ep(key: str, *, relevance: float, rank: int) -> RecallCandidate:
    return RecallCandidate(
        source=RecallSource.EPISODIC_RAW, key=key, chunk=chunk(key), relevance=relevance, rank=rank
    )


def _gn(key: str, *, relevance: float, rank: int) -> RecallCandidate:
    return RecallCandidate(
        source=RecallSource.GRAPH, key=key, node=node(key), relevance=relevance, rank=rank
    )


# ---- the confidence predicate ---------------------------------------------


def test_a_clear_standout_passes() -> None:
    cands = [_ep("a", relevance=0.9, rank=1), _ep("b", relevance=0.3, rank=2)]  # big margin
    assert passes_confidence(cands, settings=SETTINGS, question_type=QuestionType.POINT_FACT)


def test_below_the_floor_abstains() -> None:
    cands = [_ep("a", relevance=0.1, rank=1)]  # top < abstain_floor (0.30)
    assert not passes_confidence(cands, settings=SETTINGS, question_type=QuestionType.NARRATIVE)


def test_point_fact_needs_a_standout_not_diffuse_mass() -> None:
    # Many mediocre hits (high mass, tiny margin) = ambiguity → a point-fact abstains.
    cands = [_ep(f"c{i}", relevance=0.5, rank=i + 1) for i in range(4)]
    assert not passes_confidence(cands, settings=SETTINGS, question_type=QuestionType.POINT_FACT)


def test_narrative_accepts_diffuse_mass() -> None:
    # The same diffuse set surfaces for a narrative turn (context is spread across neighbours).
    cands = [_ep(f"c{i}", relevance=0.5, rank=i + 1) for i in range(4)]
    assert passes_confidence(cands, settings=SETTINGS, question_type=QuestionType.NARRATIVE)


def test_empty_set_never_passes() -> None:
    assert not passes_confidence([], settings=SETTINGS, question_type=QuestionType.NARRATIVE)


# ---- the K4 composition ---------------------------------------------------


def test_surfaces_the_post_policy_set_when_confident() -> None:
    cands = [_gn("g1", relevance=0.9, rank=1), _gn("g2", relevance=0.3, rank=2)]
    out = compose_gate(
        cands, settings=SETTINGS, question_type=QuestionType.POINT_FACT, allowlist={"g1", "g2"}
    )
    assert not out.abstained
    assert [c.key for c in out.surfaced] == ["g1", "g2"]
    assert out.reason is AbstentionReason.SURFACED


def test_safety_subtracts_before_abstention_evaluates_the_remainder() -> None:
    # The strong hit g1 is a gated (wellbeing) node NOT in the allowlist; the survivor g2 is
    # weak → after subtraction the remainder fails the gate. Confidence is judged on what
    # could surface, and g1 never appears.
    cands = [_gn("g1", relevance=0.95, rank=1), _gn("g2", relevance=0.10, rank=2)]
    out = compose_gate(
        cands, settings=SETTINGS, question_type=QuestionType.POINT_FACT, allowlist={"g2"}
    )
    assert out.abstained
    assert out.surfaced == ()  # the subtracted g1 is never surfaced
    assert out.reason is AbstentionReason.WITHHELD_BY_POLICY  # policy is the but-for cause


def test_weak_recall_abstains_with_low_confidence_reason() -> None:
    # No policy subtraction; recall is just weak → LOW_CONFIDENCE, not policy.
    cands = [_ep("a", relevance=0.15, rank=1)]
    out = compose_gate(cands, settings=SETTINGS, question_type=QuestionType.NARRATIVE)
    assert out.abstained
    assert out.reason is AbstentionReason.LOW_CONFIDENCE


def test_episodic_candidates_are_not_graph_subtracted() -> None:
    # The K4 allowlist is graph-node-scoped; an episodic chunk always passes it.
    cands = [_ep("e1", relevance=0.9, rank=1), _ep("e2", relevance=0.3, rank=2)]
    out = compose_gate(
        cands, settings=SETTINGS, question_type=QuestionType.POINT_FACT, allowlist=set()
    )
    assert not out.abstained
    assert [c.key for c in out.surfaced] == ["e1", "e2"]


def test_no_allowlist_is_the_common_ungated_turn() -> None:
    cands = [_gn("g1", relevance=0.9, rank=1)]
    out = compose_gate(cands, settings=SETTINGS, question_type=QuestionType.POINT_FACT)
    assert not out.abstained
    assert [c.key for c in out.surfaced] == ["g1"]


def test_result_budget_truncates_the_surfaced_set() -> None:
    cands = [_ep(f"e{i}", relevance=0.9 - i * 0.01, rank=i + 1) for i in range(5)]
    out = compose_gate(
        cands, settings=SETTINGS, question_type=QuestionType.NARRATIVE, result_budget=2
    )
    assert len(out.surfaced) == 2
