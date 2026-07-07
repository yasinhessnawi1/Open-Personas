"""The stubbed-skeleton structural gate (Spec K9, T2; K9-D-12).

The HARD T1/T2 gate: the fuse → rerank path produces results with a STUB reranker —
degraded (fused) ordering, never a crash, and never a hard dependency on the real P7
model. Later tasks (score/gate/contiguity/diffusion) compose around this core and must
keep it green. The reranker is injected (a Protocol), so P7's model swaps in with no
change to the skeleton — proven here by driving the whole path with the identity stub.
"""

from __future__ import annotations

from persona.recall.config import RecallSettings
from persona.recall.models import RecallLeg
from persona.recall.pipeline import run_recall
from persona.recall.rerank import IdentityReranker

from tests.unit.recall._fixtures import chunk, gist, node

SETTINGS = RecallSettings()


def test_skeleton_produces_results_across_all_structures_with_a_stub() -> None:
    legs = {
        RecallLeg.EPISODIC_RAW: [chunk("e1", distance=0.1)],
        RecallLeg.EPISODIC_GIST: [gist("gi", members=("m1",), distance=0.2)],
        RecallLeg.GRAPH_DENSE: [node("g1", distance=0.15)],
    }
    out = run_recall(
        legs=legs,
        query="what did we discuss",
        reranker=IdentityReranker(),
        settings=SETTINGS,
        owner_id="o",
        persona_id="p",
    )
    assert {c.key for c in out} == {"e1", "gi", "g1"}


def test_skeleton_returns_fused_order_under_the_identity_stub() -> None:
    # The stub does not re-order, so output is the fused ranking (degraded ordering).
    legs = {
        RecallLeg.GRAPH_DENSE: [node("strong", distance=0.05), node("weak", distance=0.9)],
    }
    out = run_recall(
        legs=legs,
        query="q",
        reranker=IdentityReranker(),
        settings=SETTINGS,
        owner_id="o",
        persona_id="p",
    )
    assert [c.key for c in out] == ["strong", "weak"]  # fused order preserved


def test_skeleton_never_crashes_on_empty_legs() -> None:
    out = run_recall(
        legs={},
        query="q",
        reranker=IdentityReranker(),
        settings=SETTINGS,
    )
    assert out == []


def test_skeleton_honours_the_rerank_top_k_split() -> None:
    legs = {RecallLeg.GRAPH_DENSE: [node(f"n{i}", distance=0.05 * i) for i in range(6)]}
    voice = run_recall(
        legs=legs,
        query="q",
        reranker=IdentityReranker(),
        settings=SETTINGS,
        owner_id="o",
        persona_id="p",
        rerank_top_k=SETTINGS.rerank_top_k_voice,
    )
    budgeted = run_recall(
        legs=legs,
        query="q",
        reranker=IdentityReranker(),
        settings=SETTINGS,
        owner_id="o",
        persona_id="p",
    )
    assert len(voice) == min(6, SETTINGS.rerank_top_k_voice)
    assert len(budgeted) == min(6, SETTINGS.result_budget)


def test_skeleton_is_reranker_agnostic() -> None:
    # A different Reranker impl (here: reverse the pool) swaps in with no skeleton change —
    # proving P7's real model needs no core edit.
    class _ReverseReranker:
        def rerank(self, query: str, candidates: list, *, top_k: int) -> list:  # type: ignore[type-arg]  # noqa: ARG002
            return list(reversed(candidates))[:top_k]

    legs = {RecallLeg.GRAPH_DENSE: [node("a", distance=0.1), node("b", distance=0.2)]}
    out = run_recall(
        legs=legs,
        query="q",
        reranker=_ReverseReranker(),
        settings=SETTINGS,
        owner_id="o",
        persona_id="p",
    )
    assert [c.key for c in out] == ["b", "a"]  # the injected reranker governed the order
