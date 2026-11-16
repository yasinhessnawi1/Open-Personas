"""Entity links given up are followed up the merge chain (R9-287 review MEDIUM-3).

A merged into B, B merged into C. B's merge copied A's entity E to C, and B's record lists it.
When B gives E up, so does C (by the same carrier rule, in SQL). This pins the pure decision;
the forget and the persona erasure are tested on Postgres in CI.
"""

from __future__ import annotations

import pytest
from persona_api.services import erasure_graph
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


class _Rows:
    def first(self) -> None:
        return None

    def all(self) -> list[object]:
        return []

    def scalars(self) -> list[object]:
        return []


class _Conn:
    def execute(self, stmt: object, params: object = None) -> _Rows:  # noqa: ARG002
        return _Rows()


class _Log:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, message: str, **_fields: object) -> None:
        self.warnings.append(message)


def test_the_chain_bound_is_reported_when_it_is_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 2 LOW-3: links still to pass up at the bound are not dropped silently."""
    log = _Log()
    monkeypatch.setattr(erasure_graph, "_log", log)

    erasure_graph._give_back_entity_links(  # noqa: SLF001
        _Conn(),  # type: ignore[arg-type]
        "u1",
        "c1",
        ["e1"],
        set(),
        depth=erasure_graph._CHAIN_LIMIT,  # noqa: SLF001
    )

    assert len(log.warnings) == 1
    assert "chain bound" in log.warnings[0]


def test_below_the_bound_nothing_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    log = _Log()
    monkeypatch.setattr(erasure_graph, "_log", log)

    erasure_graph._give_back_entity_links(  # noqa: SLF001
        _Conn(),  # type: ignore[arg-type]
        "u1",
        "c1",
        ["e1"],
        set(),
        depth=0,
    )

    assert log.warnings == []
