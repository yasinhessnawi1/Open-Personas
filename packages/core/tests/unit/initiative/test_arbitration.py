"""Unit tests — duplicate-suppression arbitration (Spec A5, T7; A5-D-6)."""

from __future__ import annotations

from persona.initiative import arbitrate_voicer


def test_most_grounded_persona_wins() -> None:
    voicer = arbitrate_voicer(
        "p-scanner", citation_provenance_personas=["p2", "p2", "p1"], activity_rank={}
    )
    assert voicer == "p2"


def test_no_provenance_falls_back_to_the_scanning_persona() -> None:
    assert arbitrate_voicer("p-scanner", citation_provenance_personas=[]) == "p-scanner"


def test_grounding_tie_breaks_by_activity() -> None:
    voicer = arbitrate_voicer(
        "p-scanner",
        citation_provenance_personas=["p1", "p2"],
        activity_rank={"p1": 3, "p2": 9},
    )
    assert voicer == "p2"


def test_full_tie_breaks_lexicographically_total_order() -> None:
    voicer = arbitrate_voicer(
        "p-scanner",
        citation_provenance_personas=["pb", "pa"],
        activity_rank={"pa": 5, "pb": 5},
    )
    assert voicer == "pa"  # deterministic — no flake, no race dependence


def test_deterministic_across_input_order() -> None:
    a = arbitrate_voicer("p", citation_provenance_personas=["x", "y", "x"], activity_rank={})
    b = arbitrate_voicer("p", citation_provenance_personas=["y", "x", "x"], activity_rank={})
    assert a == b == "x"
