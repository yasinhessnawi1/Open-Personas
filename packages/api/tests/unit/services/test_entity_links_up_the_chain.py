"""Entity links given up are followed up the merge chain (R9-287 review MEDIUM-3).

A merged into B, B merged into C. B's merge copied A's entity E to C, and B's record lists it.
When B gives E up, so does C (by the same carrier rule, in SQL). This pins the pure decision;
the forget and the persona erasure are tested on Postgres in CI.
"""

from __future__ import annotations

from persona_api.services.erasure_graph import entity_links_passed_up


def test_links_the_canonicals_own_merge_added_pass_up() -> None:
    record = {"canonical": "C", "added_entities": ["E", "F"], "run_id": "r2"}

    passed = entity_links_passed_up(record, ["E", "G"], doomed={"A"})

    assert passed == ("C", ["E"], {"canonical": "C", "added_entities": ["F"], "run_id": "r2"})


def test_a_canonical_never_merged_passes_nothing_up() -> None:
    assert entity_links_passed_up(None, ["E"], doomed=set()) is None
    assert entity_links_passed_up({"added_entities": ["E"]}, ["E"], doomed=set()) is None


def test_nothing_passes_up_to_a_canonical_that_is_going_too() -> None:
    record = {"canonical": "C", "added_entities": ["E"]}

    assert entity_links_passed_up(record, ["E"], doomed={"C"}) is None


def test_a_link_the_merge_did_not_add_does_not_pass_up() -> None:
    """C had E before B merged into it: B's record does not list it, and C keeps it."""
    record = {"canonical": "C", "added_entities": ["F"]}

    assert entity_links_passed_up(record, ["E"], doomed=set()) is None
