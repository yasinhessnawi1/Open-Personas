"""Planning a persona out of the user's graph (R9-284, D-R9-284-erase ruling 4).

The rule, on provenance trails: a node that only the persona contributed to goes; a node with
anything else keeps everything else; the SELF node never goes. The SQL that applies the plan is
Postgres only and runs in CI (tests/integration/test_erase_persona_graph_postgres.py).
"""

from __future__ import annotations

import itertools
import json

import pytest
from persona_api.services.erasure_graph import (
    delete_mirrors,
    entity_link_plan,
    merge_record,
    mirror_id_for,
    mirror_row_deletes,
    mirrors_to_remove,
    names_only_the_doomed_gave,
    orphaned_mirrors,
    plan_trails,
    source_keys,
    split_alias_list,
    unmerged,
    unsourced_mirrors,
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

    assert rewritten == [other, {**both, "reason": 'consolidation:r1 aliases:["theirs"]'}]
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
            single_row = "== canonical_id" in chain or "nodes_t.c.id == node.id" in chain
            if ".order_by(" not in chain and not single_row:
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


def test_a_doomed_name_holding_a_comma_leaves_a_legacy_reason() -> None:
    """Review HIGH-3: the pre-JSON list was joined by "," and a name may hold one."""
    entry = {**_consolidation(), "reason": "consolidation:r1 aliases:Bergen, Norway,Tromso"}

    rewritten = without_alias_names([entry], ["Bergen, Norway"], known=["Tromso"])

    assert rewritten == [{**entry, "reason": 'consolidation:r1 aliases:["Tromso"]'}]


def test_a_surviving_name_holding_a_doomed_name_as_a_fragment_stays_whole() -> None:
    entry = {**_consolidation(), "reason": "consolidation:r1 aliases:Oslo, Norway,Oslo"}

    rewritten = without_alias_names([entry], ["Oslo"], known=["Oslo, Norway", "Oslo"])

    assert rewritten == [{**entry, "reason": 'consolidation:r1 aliases:["Oslo, Norway"]'}]


def test_a_json_alias_list_is_read_as_json() -> None:
    entry = {**_consolidation(), "reason": 'consolidation:r1 aliases:["a, b", "c"]'}

    assert without_alias_names([entry], ["a, b"]) == [
        {**entry, "reason": 'consolidation:r1 aliases:["c"]'}
    ]
    assert without_alias_names([entry], ["a"]) is None, "no fragment of a name matches"


def test_split_alias_list_prefers_known_names_and_falls_back_to_commas() -> None:
    assert split_alias_list("x,y", []) == ["x", "y"]
    assert split_alias_list("a, b,c", ["a, b"]) == ["a, b", "c"]
    assert split_alias_list("a,,b,", []) == ["a", "b"]
    assert split_alias_list('["q"]', []) == ["q"]
    assert split_alias_list("[not json,z", []) == ["[not json", "z"]


def test_a_known_name_matches_only_where_it_stands_as_a_whole_name() -> None:
    """A doomed "Anna" is not read off the front of a surviving "Annas" (orchestrator mutant)."""
    assert split_alias_list("Annas,Bergen", ["Anna", "Bergen"]) == ["Annas", "Bergen"]

    survivor = {**_consolidation(), "reason": "consolidation:r1 aliases:Annas,Bergen"}
    assert without_alias_names([survivor], ["Anna"], known=["Bergen"]) is None

    both = {**_consolidation(), "reason": "consolidation:r1 aliases:Annas,Anna,Bergen"}
    assert without_alias_names([both], ["Anna"], known=["Bergen"]) == [
        {**both, "reason": 'consolidation:r1 aliases:["Annas", "Bergen"]'}
    ]


def test_a_shared_mirror_goes_only_once_none_of_its_sources_survives() -> None:
    """Round 5 MEDIUM-2: members A (P's edge, key 1) and B (Q's edge, key 2) share k->x."""
    sources = [("k::causal::x", "r1", 1), ("k::causal::x", "r1", 2)]
    rows = {("k::causal::x", "r1"): [9]}

    assert mirrors_to_remove([1], sources, rows, existing={1, 2, 9}) == [], "B's source stays"
    assert mirrors_to_remove([2], sources, rows, existing={2, 9}) == [9], "A's went before"
    assert mirrors_to_remove([1, 2], sources, rows, existing={1, 2, 9}) == [9]
    assert mirrors_to_remove([2], sources, rows, existing={1, 2, 9}) == [], "A's still there"


def test_old_single_key_sources_read_as_one_element_lists() -> None:
    assert source_keys(7) == [7]
    assert source_keys([7, 8]) == [7, 8]
    assert source_keys(None) == []
    assert source_keys(True) == []
    assert source_keys(["7", 8]) == [8]


def test_a_mirror_without_a_listed_source_falls_back_to_its_id() -> None:
    """Round 5 LOW-1: per entry, not per record."""
    newer = {"mirrored_edges": ["e1", "e2"], "mirror_sources": {"e1": [4]}}
    older = {"mirrored_edges": ["e1", "e2"]}
    whole = {"mirrored_edges": ["e1"], "mirror_sources": {"e1": [4]}}

    assert unsourced_mirrors(newer) == {"e2"}
    assert unsourced_mirrors(older) == {"e1", "e2"}
    assert unsourced_mirrors(whole) == set()


def test_a_tie_between_readings_goes_to_the_one_naming_more_doomed_names() -> None:
    """Round 5 LOW-2: known {a, b, "a,b"}; on a legacy row privacy wins."""
    known = ["a", "b", "a,b"]
    assert split_alias_list("a,b", known) == ["a,b"], "no doomed name: the fewest tokens"
    assert split_alias_list("a,b", known, doomed=["a"]) == ["a", "b"]
    assert split_alias_list("a,b", known, doomed=["a,b"]) == ["a,b"]

    entry = {**_consolidation(), "reason": "consolidation:r1 aliases:a,b"}
    assert without_alias_names([entry], ["a"], known=["b", "a,b"]) == [
        {**entry, "reason": 'consolidation:r1 aliases:["b"]'}
    ]
    assert without_alias_names([entry], ["a,b"], known=["a", "b"]) == []


def test_a_long_legacy_list_is_tokenised_fast() -> None:
    """Round 5 LOW-3: 1000 known names, about 47k characters, well inside a second."""
    import time  # noqa: PLC0415

    names = [f"name {i:04d}, of the place number {i:04d} in a town" for i in range(1000)]
    listed = ",".join(names)
    assert 40_000 < len(listed) < 50_000

    started = time.perf_counter()
    found = split_alias_list(listed, names, doomed=names[:10])
    elapsed = time.perf_counter() - started

    assert found == names
    assert elapsed < 1.0, f"took {elapsed:.2f}s"


@pytest.mark.parametrize("order", list(itertools.permutations(range(4))))
def test_a_chain_is_resolved_whatever_order_the_records_come_in(order: tuple[int, ...]) -> None:
    """Reviewer's E7: a single pass found a chain only when its records came in chain order."""
    chain = [
        ("k1::causal::x", "r1", 10),  # the persona's edge, row 10
        ("k2::causal::x", "r2", 11),  # k1's mirror row
        ("k3::causal::x", "r3", 12),  # k2's mirror row
        ("k4::causal::x", "r4", 13),  # k3's mirror row
    ]
    rows = {(m, r): [key + 1] for m, r, key in chain}

    assert mirrors_to_remove([10], [chain[i] for i in order], rows) == [11, 12, 13, 14]


_M = ("m", "r")


def test_the_reviewers_two_calls_the_seed_rule_alone_keeps_a_stale_mirror() -> None:
    """Round 6 M-1: A (key 1, P's) and B (key 2) share M; B is pending, P is erased, then Q.

    The seed rule keeps M while B's source lives, which is right; but once B is given back
    and A's stale listing is all that names M, erasing Q finds no source of M going, so the
    seed rule alone would keep M for ever. ``orphaned_mirrors`` is what removes it.
    """
    assert mirrors_to_remove({1}, [("m", "r", 1), ("m", "r", 2)], {_M: {9}}, {1, 2, 9}) == []
    assert mirrors_to_remove({2}, [("m", "r", 1)], {_M: {9}}, {2, 9}) == []

    stale = {"run_id": "r", "mirrored_edges": ["m"], "mirror_sources": {"m": [1]}}
    doomed, rewrites = orphaned_mirrors({"a": stale}, {_M: {9}}, existing={2, 9})

    assert doomed == [9]
    assert rewrites == {"a": {**stale, "mirrored_edges": [], "mirror_sources": {}}}


def test_a_listing_keeps_only_its_live_sources_and_a_backed_mirror_stays() -> None:
    a = {"run_id": "r", "mirrored_edges": ["m", "old"], "mirror_sources": {"m": [1, 3]}}
    b = {"run_id": "r", "mirrored_edges": ["m"], "mirror_sources": {"m": 2}}

    doomed, rewrites = orphaned_mirrors({"a": a, "b": b}, {_M: {9}}, existing={2, 3, 9})

    assert doomed == [], "B's source and A's key 3 still back m"
    assert rewrites == {"a": {**a, "mirror_sources": {"m": [3]}}}, "old (no sources) is kept"
    assert orphaned_mirrors({"b": b}, {_M: {9}}, existing={2, 9}) == ([], {}), "nothing stale"


def test_fewer_unexplained_pieces_outrank_more_doomed_names() -> None:
    """Reviewer's E6: the doomed-name tie-break applies only between equally explained readings.

    "q,a" read whole is a known name; read as q and a it holds the doomed "a" but leaves "q"
    unexplained, so it loses.
    """
    assert split_alias_list("q,a", ["q,a", "a"], doomed=["a"]) == ["q,a"]


class _CountingSlices(str):
    """A legacy list that counts how many candidate tokens are cut from it."""

    __slots__ = ()
    cuts: list[int] = []  # noqa: RUF012 - one shared counter for the test

    def __getitem__(self, key: object) -> str:  # type: ignore[override]
        if isinstance(key, slice):
            _CountingSlices.cuts.append(1)
        return str.__getitem__(self, key)  # type: ignore[index]


def test_a_known_name_is_tried_only_up_to_the_longest_known_name() -> None:
    """Reviewer's E7: without the bound every later comma is tried, O(n^2) cuts."""
    names = [f"n{i:03d}" for i in range(300)]
    _CountingSlices.cuts.clear()

    found = split_alias_list(_CountingSlices(",".join(names)), names)

    assert found == names
    assert len(_CountingSlices.cuts) <= 3 * len(names), len(_CountingSlices.cuts)


def test_a_json_list_with_a_non_string_entry_is_not_taken_as_json() -> None:
    """Reviewer's E11: read as a legacy comma list instead, never trusted as names."""
    assert split_alias_list('["a", 1]', []) == ['["a"', " 1]"]
    assert split_alias_list('["a", null]', ["a"]) == ['["a"', " null]"]


def test_an_orphan_chain_is_pruned_to_its_end() -> None:
    """Round 7 LOW-1, the reviewer's chain: M1 (run r1) from a dead key 100 is the only source
    of M2 (run r2), whose source is M1's row 11. One pass found M1 only."""
    x = {"run_id": "r1", "mirrored_edges": ["m1"], "mirror_sources": {"m1": [100]}}
    c1 = {"run_id": "r2", "mirrored_edges": ["m2"], "mirror_sources": {"m2": [11]}}
    rows = {("m1", "r1"): {11}, ("m2", "r2"): {12}}

    doomed, rewrites = orphaned_mirrors({"x": x, "c1": c1}, rows, existing={11, 12})

    assert doomed == [11, 12]
    assert rewrites["c1"]["mirrored_edges"] == [], "M2 lost its source, M1's row"
    assert rewrites["x"]["mirrored_edges"] == []


def test_a_mirror_row_is_deleted_only_under_its_own_runs_tag() -> None:
    """Round 7 LOW-2: one DELETE per run, each matching that run's consolidation tag."""
    from sqlalchemy.dialects import postgresql  # noqa: PLC0415

    rows = {("m", "r1"): {9}, ("m", "r2"): {10}, ("n", "r1"): {11}}

    statements = mirror_row_deletes("u1", [9, 10, 11, 99], rows)

    params = [s.compile(dialect=postgresql.dialect()).params for s in statements]
    assert len(statements) == 2, "one per run; key 99 is no mirror row and is not deleted"
    assert [(p["param_1"], p["param_2"]) for p in params] == [
        ([9, 11], {"source": "system", "reason": "consolidation:r1"}),
        ([10], {"source": "system", "reason": "consolidation:r2"}),
    ]
    assert mirror_row_deletes("u1", [], rows) == []


def test_a_large_key_set_is_one_array_parameter() -> None:
    """Round 7 LOW-3: = ANY(:array), not an IN list with a parameter per key."""
    from sqlalchemy.dialects import postgresql  # noqa: PLC0415

    keys = list(range(5000))
    (statement,) = mirror_row_deletes("u1", keys, {("m", "r1"): set(keys)})

    compiled = statement.compile(dialect=postgresql.dialect())
    assert "= ANY (" in str(compiled)
    assert len(compiled.params) == 3, "owner, the key array and the tag"


def test_the_prune_never_takes_another_runs_mirror_with_the_same_id() -> None:
    """Reviewer's E3: run r1's m lost its source; run r2's m (same id) still has one."""
    r1 = {"run_id": "r1", "mirrored_edges": ["m"], "mirror_sources": {"m": [1]}}
    r2 = {"run_id": "r2", "mirrored_edges": ["m"], "mirror_sources": {"m": [2]}}
    rows = {("m", "r1"): {9}, ("m", "r2"): {10}}

    doomed, _ = orphaned_mirrors({"a": r1, "b": r2}, rows, existing={2, 9, 10})

    assert doomed == [9]
    (statement,) = mirror_row_deletes("u1", doomed, rows)
    assert "consolidation:r1" in str(statement.compile().params)


def test_a_link_another_live_member_carries_stays_and_passes_to_it() -> None:
    """Round 8 LOW-4: the entity version of the shared mirror."""
    remove, transfers = entity_link_plan(["e1", "e2", "e1"], {"e2": "b"})

    assert remove == ["e1"]
    assert transfers == {"b": ["e2"]}
    assert entity_link_plan([], {"e": "b"}) == ([], {})
