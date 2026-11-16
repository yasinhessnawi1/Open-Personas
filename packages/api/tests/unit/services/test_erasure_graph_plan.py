"""Planning a persona out of the user's graph (R9-284, D-R9-284-erase ruling 4).

The rule, on provenance trails: a node that only the persona contributed to goes; a node with
anything else keeps everything else; the SELF node never goes. The SQL that applies the plan is
Postgres only and runs in CI (tests/integration/test_erase_persona_graph_postgres.py).
"""

from __future__ import annotations

import json

from persona_api.services.erasure_graph import (
    delete_mirrors,
    merge_record,
    mirror_id_for,
    mirrors_to_remove,
    names_only_the_doomed_gave,
    plan_trails,
    unmerged,
    without_alias_names,
    without_aliases,
    without_persona_aliases,
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


def test_an_entity_loses_the_surfaces_the_persona_gave_and_keeps_the_rest() -> None:
    """Review H-1: an alias with no persona (older than the field) is not guessed at."""
    mine = {"surface": "my doctor", "confidence": 0.9, "persona_id": _PERSONA}
    theirs = {"surface": "the GP", "confidence": None, "persona_id": _OTHER}
    unknown = {"surface": "Dr. Hansen", "confidence": None}

    assert without_persona_aliases([mine, theirs, unknown], _PERSONA) == [theirs, unknown]
    assert without_persona_aliases([theirs, unknown], _PERSONA) is None
    assert without_persona_aliases([], _PERSONA) is None


def test_a_consolidation_entry_stops_listing_the_personas_names() -> None:
    """Review H-3: _accrete_aliases wrote the member's name into the canonical's trail too."""
    other = _entry(_OTHER)
    both = {**_consolidation(), "reason": "consolidation:r1 aliases:mine,theirs"}
    only_mine = {**_consolidation(), "reason": "consolidation:r2 aliases:mine"}

    rewritten = without_alias_names([other, both, only_mine], ["mine"])

    assert rewritten == [other, {**both, "reason": "consolidation:r1 aliases:theirs"}]
    assert without_alias_names([other, both], ["nobody"]) is None
    assert (
        without_alias_names([{**_entry(_OTHER), "reason": "said aliases:mine"}], ["mine"]) is None
    )


def test_a_mirror_is_deleted_only_while_it_is_still_the_merges() -> None:
    """Review M-4: another persona may have asserted the same fact since, under the same id."""
    from sqlalchemy.dialects import postgresql  # noqa: PLC0415

    stmt = delete_mirrors("user_alice", ["k::causal::x"], run_id="r3")
    compiled = stmt.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "@>" in sql, sql
    assert "invalid_at IS NULL" in sql
    assert {"source": "system", "reason": "consolidation:r3"} in compiled.params.values()
    assert ["k::causal::x"] in compiled.params.values()


def test_the_mirror_id_is_the_one_consolidation_gave() -> None:
    """Review M-3: the member's end re-pointed onto the canonical, as the merge act does."""
    out = mirror_id_for("m", "x", "causal", member_id="m", canonical_id="k")
    into = mirror_id_for("x", "m", "temporal", member_id="m", canonical_id="k")

    assert out == "k::causal::x"
    assert into == "x::temporal::k"
    assert mirror_id_for("m", "k", "causal", member_id="m", canonical_id="k") is None
    assert mirror_id_for("a", "b", "causal", member_id="m", canonical_id="k") is None


def test_every_row_lock_the_graph_step_takes_is_taken_in_a_fixed_order() -> None:
    """Review LOW: two transactions locking overlapping rows in any order can deadlock."""
    import ast  # noqa: PLC0415
    import inspect  # noqa: PLC0415

    from persona_api.services import erasure_graph  # noqa: PLC0415

    tree = ast.parse(inspect.getsource(erasure_graph))
    locked = unordered = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "with_for_update":
            locked += 1
            chain = ast.unparse(node.value)
            if ".order_by(" not in chain and "== canonical_id" not in chain:
                unordered += 1
    assert locked >= 5
    assert unordered == 0


def test_a_name_a_surviving_member_also_gave_stays() -> None:
    """Review LOW: the alias is the canonical's while any member that stays carries it."""
    assert names_only_the_doomed_gave(["oslo", "bergen", ""], ["oslo"]) == ["bergen"]
    assert names_only_the_doomed_gave(["oslo"], ["oslo"]) == []
    assert names_only_the_doomed_gave(["b", "a", "a"], []) == ["a", "b"]


def test_mirrors_are_found_by_their_source_row_and_followed_down_a_chain() -> None:
    """Review LOW-3: a mirror of a mirror (its canonical merged again) goes with the first."""
    sources = [
        ("k1::causal::x", "r1", 10),  # mirrors the persona's edge, row 10
        ("k2::causal::x", "r2", 11),  # mirrors k1's mirror, whose row (now closed) is 11
        ("k3::causal::y", "r3", 99),  # mirrors someone else's edge
    ]
    rows = {
        ("k1::causal::x", "r1"): [11],
        ("k2::causal::x", "r2"): [12],
        ("k3::causal::y", "r3"): [13],
    }

    assert mirrors_to_remove([10], sources, rows) == [11, 12]
    assert mirrors_to_remove([], sources, rows) == []
    assert mirrors_to_remove([10], sources, {}) == [], "no mirror row left to remove"
    assert mirrors_to_remove([10], sources[:1], rows) == [11], "the chain ends at k1"
