"""Planning a persona out of the user's graph (R9-284, D-R9-284-erase ruling 4).

The rule, on provenance trails: a node that only the persona contributed to goes; a node with
anything else keeps everything else; the SELF node never goes. The SQL that applies the plan is
Postgres only and runs in CI (tests/integration/test_erase_persona_graph_postgres.py).
"""

from __future__ import annotations

import json

from persona_api.services.erasure_graph import (
    merge_record,
    plan_trails,
    unmerged,
    without_aliases,
)

_PERSONA = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_OTHER = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_SELF = "user_alice::self"
_AT = "2026-11-15T12:00:00Z"


def _entry(persona_id: str | None, source: str = "system") -> dict[str, object]:
    return {
        "source": source,
        "persona_id": persona_id,
        "interaction_id": "conv1" if persona_id else None,
        "written_at": _AT,
        "grounding": f"what {persona_id} heard" if persona_id else None,
        "reason": "because",
        "superseded_content": "what it said before",
    }


def _plan(*rows: tuple[str, list[dict[str, object]]]):  # noqa: ANN202
    return plan_trails(
        ((key, key, trail) for key, trail in rows), persona_id=_PERSONA, self_node_id=_SELF
    )


def test_a_node_only_the_persona_contributed_to_is_deleted() -> None:
    plan = _plan(("n1", [_entry(_PERSONA), _entry(_PERSONA)]))

    assert plan.doomed == ["n1"]
    assert plan.rewrites == {}


def test_a_shared_node_keeps_everything_but_the_persona() -> None:
    other, user = _entry(_OTHER), _entry(None, source="user")
    plan = _plan(("n1", [_entry(_PERSONA), other, _entry(_PERSONA), user]))

    assert plan.doomed == []
    assert plan.rewrites == {"n1": [other, user]}


def test_a_user_edit_makes_a_node_the_users_own() -> None:
    """An entry with no persona (the user's edit, or synthesis with none) is not the persona's."""
    user = _entry(None, source="user")
    plan = _plan(("n1", [_entry(_PERSONA), user]))

    assert plan.rewrites == {"n1": [user]}


def test_a_node_without_the_persona_is_left_alone() -> None:
    plan = _plan(("n1", [_entry(_OTHER)]), ("n2", [_entry(None)]))

    assert plan.doomed == []
    assert plan.rewrites == {}


def test_the_self_node_is_never_deleted_and_keeps_nothing_of_the_persona() -> None:
    plan = _plan((_SELF, [_entry(_PERSONA), _entry(_PERSONA, source="persona_self")]))

    assert plan.doomed == []
    assert plan.rewrites == {
        _SELF: [
            {"source": "system", "written_at": _AT},
            {"source": "persona_self", "written_at": _AT},
        ]
    }


def test_version_rows_are_judged_by_their_own_trail_and_their_node() -> None:
    """A version key is not a node id; the SELF exception follows the node the version is of."""
    plan = plan_trails(
        [
            ("v1", "n1", [_entry(_PERSONA)]),
            ("v2", _SELF, [_entry(_PERSONA)]),
            ("v3", "n1", [_entry(_PERSONA), _entry(_OTHER)]),
        ],
        persona_id=_PERSONA,
        self_node_id=_SELF,
    )

    assert plan.doomed == ["v1"]
    assert plan.rewrites == {
        "v2": [{"source": "system", "written_at": _AT}],
        "v3": [_entry(_OTHER)],
    }


def test_a_malformed_entry_is_never_taken_for_the_persona() -> None:
    plan = _plan(("n1", ["not an entry", _entry(_PERSONA)]))  # type: ignore[list-item]

    assert plan.rewrites == {"n1": ["not an entry"]}


def _consolidation(run: str = "r1") -> dict[str, object]:
    """What ConsolidationPass appends when near-duplicates merge (_accrete_aliases)."""
    return {
        "source": "system",
        "persona_id": None,
        "written_at": _AT,
        "reason": f"consolidation:{run} aliases:a name",
    }


def test_consolidation_bookkeeping_does_not_make_a_node_shared() -> None:
    """Review H3: a node only the persona fed is deleted however often it merged."""
    plan = _plan(("n1", [_entry(_PERSONA), _consolidation(), _consolidation("r2")]))

    assert plan.doomed == ["n1"]


def test_a_real_contribution_beside_consolidation_keeps_the_node() -> None:
    other, user = _entry(_OTHER), _entry(None, source="user")
    plan = _plan(
        ("n1", [_entry(_PERSONA), _consolidation(), other]),
        ("n2", [_entry(_PERSONA), _consolidation(), user]),
    )

    assert plan.doomed == []
    assert plan.rewrites == {"n1": [_consolidation(), other], "n2": [_consolidation(), user]}


def test_a_system_entry_that_is_not_consolidation_is_independent() -> None:
    synthesis = {"source": "system", "persona_id": None, "written_at": _AT, "reason": "k2"}
    plan = _plan(("n1", [_entry(_PERSONA), synthesis]))

    assert plan.rewrites == {"n1": [synthesis]}


def test_the_self_node_loses_consolidations_entries_with_the_personas() -> None:
    plan = _plan((_SELF, [_entry(_PERSONA), _consolidation()]))

    assert plan.doomed == []
    assert plan.rewrites == {_SELF: [{"source": "system", "written_at": _AT}]}


def test_a_merge_record_is_read_only_when_it_is_one() -> None:
    record = {"run_id": "r1", "canonical": "c", "mirrored_edges": ["e1"], "added_entities": []}

    assert merge_record({"_k7_merge": json.dumps(record)}) == record
    assert merge_record({"_k7_merge": "not json"}) is None
    assert merge_record({"_k7_merge": "[1]"}) is None
    assert merge_record({}) is None
    assert merge_record(None) is None


def test_a_name_leaves_the_alias_list_and_the_rest_stays() -> None:
    metadata = {"_k7_aliases": json.dumps(["mine", "theirs"]), "other": "x"}

    assert without_aliases(metadata, ["mine"]) == {
        "_k7_aliases": json.dumps(["theirs"]),
        "other": "x",
    }
    assert without_aliases(metadata, ["mine", "theirs"]) == {"other": "x"}
    assert without_aliases(metadata, ["nobody"]) is None
    assert without_aliases({"other": "x"}, ["mine"]) is None


def test_only_an_entry_with_no_persona_is_bookkeeping() -> None:
    """An entry naming another persona is that persona's, whatever its reason says."""
    theirs = {**_consolidation(), "persona_id": _OTHER}
    plan = _plan(("n1", [_entry(_PERSONA), theirs]))

    assert plan.rewrites == {"n1": [theirs]}


def test_unmerging_reopens_the_runs_edges_and_drops_only_the_record() -> None:
    """Review M1: the inverse ConsolidationPass.unmerge applies to a member of a doomed node."""
    record = {"run_id": "r7", "canonical": "c", "mirrored_edges": [], "added_entities": []}
    metadata = {"_k7_merge": json.dumps(record), "kept": "yes"}

    assert unmerged(metadata) == ("consolidation:r7", {"kept": "yes"})


def test_a_member_without_a_usable_record_is_still_freed() -> None:
    assert unmerged({"kept": "yes"}) == (None, {"kept": "yes"})
    assert unmerged({"_k7_merge": json.dumps({"canonical": "c"})}) == (None, {})
    assert unmerged(None) == (None, {})
