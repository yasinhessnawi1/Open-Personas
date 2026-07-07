"""Temporal contiguity + the DrillStop contract (Spec K9, T4; K9-D-6/D-7).

Pins: the shared question-type classifier, the reserved k_c budget seeded from the top
hit(s), forward-asymmetric neighbour order, the four DrillStop guards (budget, similarity
drop-off, gist boundary, question-type), and the reinforcement-through-a-gist member ids.
"""

from __future__ import annotations

from persona.recall.config import RecallSettings
from persona.recall.contiguity import (
    DrillStop,
    Episode,
    QuestionType,
    classify_question_type,
    expand_contiguity,
)
from persona.recall.models import RecallCandidate, RecallSource

from tests.unit.recall._fixtures import chunk

# ---- the shared classifier (consumed by T4 contiguity AND T6 gate) --------


def test_narrative_cue_classifies_narrative() -> None:
    assert classify_question_type("what happened that day with the move?") is QuestionType.NARRATIVE
    assert (
        classify_question_type("tell me about when we planned the trip") is QuestionType.NARRATIVE
    )


def test_point_fact_query_classifies_point_fact() -> None:
    assert classify_question_type("what is my dentist's name") is QuestionType.POINT_FACT
    assert classify_question_type("my sister's phone number") is QuestionType.POINT_FACT


# ---- DrillStop guards -----------------------------------------------------


def test_drillstop_budget_guard_stops_at_k_c() -> None:
    stop = DrillStop(budget=2, beta=0.5)
    assert not stop.stopped(added=1, seed_relevance=0.9, neighbour_relevance=None)
    assert stop.stopped(added=2, seed_relevance=0.9, neighbour_relevance=None)  # budget reached


def test_drillstop_similarity_dropoff_stops_when_a_reading_exists() -> None:
    stop = DrillStop(budget=10, beta=0.5)
    # neighbour relevance 0.2 < 0.5*0.9 → stop; a pure temporal neighbour (None) does not.
    assert stop.stopped(added=0, seed_relevance=0.9, neighbour_relevance=0.2)
    assert not stop.stopped(added=0, seed_relevance=0.9, neighbour_relevance=None)


# ---- expansion ------------------------------------------------------------


def _seed(key: str, *, relevance: float = 0.9, rank: int = 1) -> RecallCandidate:
    return RecallCandidate(
        source=RecallSource.EPISODIC_RAW, key=key, chunk=chunk(key), relevance=relevance, rank=rank
    )


class _FakeEpisodes:
    def __init__(self, mapping: dict[str, Episode]) -> None:
        self._mapping = mapping

    def episode(self, candidate: RecallCandidate) -> Episode | None:
        return self._mapping.get(candidate.key)


def test_point_fact_turn_expands_nothing() -> None:
    members = tuple(chunk(f"m{i}") for i in range(5))
    provider = _FakeEpisodes({"seed": Episode(members=members, seed_index=2)})
    new, used = expand_contiguity(
        [_seed("seed")],
        provider=provider,
        settings=RecallSettings(),
        question_type=QuestionType.POINT_FACT,
    )
    assert new == []  # guard 4: k_c → 0
    assert used == []


def test_narrative_turn_expands_forward_biased_within_budget() -> None:
    members = tuple(chunk(f"m{i}") for i in range(6))  # seed at index 2
    provider = _FakeEpisodes({"m2": Episode(members=members, seed_index=2)})
    seed = _seed("m2")
    new, used = expand_contiguity(
        [seed],
        provider=provider,
        settings=RecallSettings(contiguity_budget=3),
        question_type=QuestionType.NARRATIVE,
    )
    # Forward-biased from index 2: m3, m1, m4 (next-before-previous), capped at k_c=3.
    assert [c.key for c in new] == ["m3", "m1", "m4"]
    assert used == ["m3", "m1", "m4"]  # the reinforcement-through-a-gist member ids
    assert all(c.via_contiguity for c in new)


def test_expansion_never_crosses_the_gist_boundary() -> None:
    # Only 2 members exist besides the seed → expansion stops at the boundary, never invents.
    members = (chunk("m0"), chunk("seed"), chunk("m2"))
    provider = _FakeEpisodes({"seed": Episode(members=members, seed_index=1)})
    new, _ = expand_contiguity(
        [_seed("seed")],
        provider=provider,
        settings=RecallSettings(contiguity_budget=10),
        question_type=QuestionType.NARRATIVE,
    )
    assert {c.key for c in new} == {"m0", "m2"}  # bounded by the episode members (guard 3)


def test_a_gist_seed_walks_its_whole_episode_from_the_start() -> None:
    members = tuple(chunk(f"m{i}") for i in range(4))
    gist_seed = RecallCandidate(
        source=RecallSource.EPISODIC_GIST,
        key="g",
        chunk=chunk("g", text="gist"),
        relevance=0.9,
        rank=1,
    )
    provider = _FakeEpisodes({"g": Episode(members=members, seed_index=None)})
    new, _ = expand_contiguity(
        [gist_seed],
        provider=provider,
        settings=RecallSettings(contiguity_budget=2),
        question_type=QuestionType.NARRATIVE,
    )
    assert [c.key for c in new] == ["m0", "m1"]  # walk from the start, capped at budget


def test_expansion_dedupes_against_the_existing_pool() -> None:
    members = (chunk("m0"), chunk("seed"), chunk("m2"))
    provider = _FakeEpisodes({"seed": Episode(members=members, seed_index=1)})
    scored = [_seed("seed"), _seed("m2", relevance=0.4, rank=2)]  # m2 already present
    new, _ = expand_contiguity(
        scored,
        provider=provider,
        settings=RecallSettings(contiguity_budget=10),
        question_type=QuestionType.NARRATIVE,
    )
    assert [c.key for c in new] == ["m0"]  # m2 not re-added


def test_no_episode_means_no_expansion_failsoft() -> None:
    provider = _FakeEpisodes({})  # seed has no covering gist
    new, used = expand_contiguity(
        [_seed("seed")],
        provider=provider,
        settings=RecallSettings(contiguity_budget=5),
        question_type=QuestionType.NARRATIVE,
    )
    assert new == []
    assert used == []
