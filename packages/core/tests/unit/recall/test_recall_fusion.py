"""Fuse-don't-route: N heterogeneous legs → one ranked set (Spec K9, T2; K9-D-1/2).

The architectural spine. Pins: cross-structure fusion (an answer living episodic-only,
graph-only, or in both is recalled through the ONE path — acceptance-1), no-gating (a
single-leg hit still ranks), hybrid-justifies-itself (a multi-leg hit outranks single-leg),
per-source quotas (a flooding leg can't crowd the pool), relevance = 1 − distance, the two
scoping keys stamped and never crossed, deterministic tie-break, and empty-legs → empty.
"""

from __future__ import annotations

from persona.recall.config import RecallSettings
from persona.recall.fusion import fuse
from persona.recall.models import RecallLeg, RecallSource

from tests.unit.recall._fixtures import chunk, gist, node

SETTINGS = RecallSettings()


def _keys(results: list) -> list[str]:  # type: ignore[type-arg]
    return [r.key for r in results]


def test_acceptance_1_episodic_graph_and_both_all_recalled_through_one_path() -> None:
    # An answer that lives episodic-only ("e_only"), graph-only ("g_only"), and in both
    # structures ("shared" appears as a chunk AND a node with distinct ids) — all surface
    # through the single fuse, no routing branch decides between structures.
    legs = {
        RecallLeg.EPISODIC_RAW: [chunk("e_only", distance=0.1), chunk("e_shared", distance=0.2)],
        RecallLeg.GRAPH_DENSE: [node("g_only", distance=0.1), node("g_shared", distance=0.2)],
    }
    out = fuse(legs=legs, settings=SETTINGS, owner_id="o", persona_id="p", top_k=10)
    keys = set(_keys(out))
    assert {"e_only", "g_only", "e_shared", "g_shared"} <= keys
    sources = {r.key: r.source for r in out}
    assert sources["e_only"] is RecallSource.EPISODIC_RAW
    assert sources["g_only"] is RecallSource.GRAPH


def test_a_node_in_one_leg_only_still_ranks() -> None:
    legs = {RecallLeg.GRAPH_SPARSE: [node("g1")]}
    out = fuse(legs=legs, settings=SETTINGS, owner_id="o", persona_id="p", top_k=10)
    assert _keys(out) == ["g1"]
    assert out[0].graph_sparse_rank == 1
    assert out[0].graph_dense_rank is None  # single-leg — observable


def test_a_candidate_in_two_graph_legs_outranks_a_single_leg_one() -> None:
    legs = {
        RecallLeg.GRAPH_DENSE: [node("both", distance=0.2), node("dense_only", distance=0.1)],
        RecallLeg.GRAPH_SPARSE: [node("both")],
    }
    out = fuse(legs=legs, settings=SETTINGS, owner_id="o", persona_id="p", top_k=10)
    assert out[0].key == "both"  # two-leg contribution beats a single strong leg at rank 1
    assert out[0].graph_dense_rank == 1
    assert out[0].graph_sparse_rank == 1


def test_the_dense_object_wins_so_distance_is_preserved() -> None:
    # "both" appears sparse-first (no distance) then dense (distance); the dense copy
    # must be kept so relevance is available.
    legs = {
        RecallLeg.GRAPH_SPARSE: [node("both")],
        RecallLeg.GRAPH_DENSE: [node("both", distance=0.25)],
    }
    out = fuse(legs=legs, settings=SETTINGS, owner_id="o", persona_id="p", top_k=10)
    assert out[0].relevance == 0.75
    assert out[0].node is not None
    assert out[0].node.distance == 0.25


def test_per_source_quota_stops_a_leg_flooding_the_pool() -> None:
    s = RecallSettings(quota_graph_dense=2)
    flood = [node(f"n{i}", distance=0.1 * i) for i in range(5)]
    out = fuse(
        legs={RecallLeg.GRAPH_DENSE: flood},
        settings=s,
        owner_id="o",
        persona_id="p",
        top_k=10,
    )
    assert _keys(out) == ["n0", "n1"]  # only the leg's top-2 were eligible


def test_scoping_keys_are_stamped_and_never_crossed() -> None:
    legs = {
        RecallLeg.EPISODIC_RAW: [chunk("e1", distance=0.1)],
        RecallLeg.GRAPH_DENSE: [node("g1", distance=0.1)],
    }
    out = fuse(legs=legs, settings=SETTINGS, owner_id="owner-x", persona_id="persona-y", top_k=10)
    by_key = {r.key: r for r in out}
    assert by_key["e1"].persona_id == "persona-y"
    assert by_key["e1"].owner_id is None  # episodic never carries an owner scope
    assert by_key["g1"].owner_id == "owner-x"
    assert by_key["g1"].persona_id is None  # graph never carries a persona scope


def test_relevance_is_none_for_a_sparse_only_hit() -> None:
    out = fuse(
        legs={RecallLeg.GRAPH_SPARSE: [node("g1")]},
        settings=SETTINGS,
        owner_id="o",
        persona_id="p",
        top_k=10,
    )
    assert out[0].relevance is None


def test_ties_break_by_key_ascending_for_determinism() -> None:
    # Equal single-leg contributions at the same rank → deterministic key order.
    legs = {RecallLeg.EPISODIC_RAW: [chunk("b"), chunk("a")]}
    # Both at their own ranks (1, 2); to force a tie put each in its own leg at rank 1.
    legs = {
        RecallLeg.EPISODIC_RAW: [chunk("b")],
        RecallLeg.EPISODIC_GIST: [gist("a", members=("m1",))],
    }
    out = fuse(legs=legs, settings=SETTINGS, owner_id="o", persona_id="p", top_k=10)
    assert _keys(out) == ["a", "b"]  # equal score → key ascending


def test_gist_leg_produces_gist_source_candidates() -> None:
    out = fuse(
        legs={RecallLeg.EPISODIC_GIST: [gist("gi", members=("m1", "m2"), distance=0.1)]},
        settings=SETTINGS,
        owner_id="o",
        persona_id="p",
        top_k=10,
    )
    assert out[0].source is RecallSource.EPISODIC_GIST
    assert out[0].chunk is not None
    assert out[0].chunk.member_ids == ("m1", "m2")  # the drill pointers survive fusion


def test_top_k_truncates_the_fused_pool() -> None:
    legs = {RecallLeg.GRAPH_DENSE: [node(f"n{i}", distance=0.05 * i) for i in range(6)]}
    out = fuse(legs=legs, settings=SETTINGS, owner_id="o", persona_id="p", top_k=3)
    assert len(out) == 3
    assert [r.rank for r in out] == [1, 2, 3]


def test_empty_legs_fuse_to_empty() -> None:
    assert fuse(legs={}, settings=SETTINGS, owner_id="o", persona_id="p", top_k=10) == []
