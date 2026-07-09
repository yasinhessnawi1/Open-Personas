"""Unified recall composition — projection, abstention, fail-soft (Spec K9, T8/T9).

Pins the HARD conditions: the K9 path projects into the existing episodic/graph seam
(K9-D-11); abstention ⇒ empty (honest absence, K9-D-9); a whole-path failure degrades to
memoryless, never turn-fatal (K9-D-3/D-4); and the D-12 stubbed-skeleton property holds
end-to-end on the composition (a stub reranker yields fused order, no hard P7 dep).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.graph.fusion import HybridResult
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.recall.config import RecallSettings
from persona.recall.rerank import IdentityReranker
from persona.schema.chunks import PersonaChunk, WriteSource
from persona.stores.lifecycle import EpisodicSettings
from persona_runtime.unified_recall import UnifiedProjection, make_unified_recall

if TYPE_CHECKING:
    from collections.abc import Callable

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


def _chunk(cid: str, *, distance: float, text: str = "t") -> PersonaChunk:
    return PersonaChunk(id=cid, text=text, distance=distance, created_at=NOW)


def _node(nid: str, *, distance: float) -> ConceptNode:
    return ConceptNode(
        id=nid,
        node_kind=NodeKind.FACT,
        concept_name=nid,
        content="c",
        distance=distance,
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),),
        created_at=NOW,
    )


def _hybrid(nid: str, *, distance: float, rank: int) -> HybridResult:
    return HybridResult(
        node=_node(nid, distance=distance), score=1.0 / rank, rank=rank, dense_rank=rank
    )


def _make(**over: object) -> Callable[[str], UnifiedProjection]:
    base: dict[str, object] = {
        "reranker": IdentityReranker(),
        "settings": RecallSettings(),
        "episodic_settings": EpisodicSettings(),
        "persona_id": "p",
        "owner_provider": lambda: "owner-1",
        "episodic_query": lambda _q, _k: [_chunk("e1", distance=0.02, text="my address is 5 Oak")],
        "resolve_display": list,
        "graph_retrieve": lambda _q: [_hybrid("g1", distance=0.4, rank=1)],
        "now": lambda: NOW,
    }
    base.update(over)
    return make_unified_recall(**base)  # type: ignore[arg-type]


def test_projects_episodic_and_graph_into_the_seam() -> None:
    recall = _make()
    out = recall("what is my address")
    assert not out.abstained
    assert [c.id for c in out.episodic] == ["e1"]  # episodic candidate → chunk
    assert any(i.concept_name == "g1" for i in out.graph.items)  # graph candidate → item
    assert "e1" in out.episodic_recalled_ids  # reinforcement flows through


def test_abstention_yields_an_empty_memoryless_projection() -> None:
    # A weak-only pool → the gate abstains → nothing surfaces (honest absence).
    recall = _make(
        episodic_query=lambda _q, _k: [_chunk("weak", distance=0.95)],
        graph_retrieve=None,
    )
    out = recall("what is my passport number")
    assert out.abstained
    assert out.episodic == []
    assert out.graph.items == ()
    assert out.episodic_recalled_ids == ()


def test_a_failure_degrades_to_memoryless_never_turn_fatal() -> None:
    def _boom(_q: str, _k: int) -> list[PersonaChunk]:
        raise RuntimeError("store down")

    out = _make(episodic_query=_boom)("anything")
    assert out.abstained is False  # a memoryless default, not an abstention verdict
    assert out.episodic == []
    assert out.graph.items == ()


def test_stub_reranker_produces_results_end_to_end_no_p7_dep() -> None:
    # K9-D-12 on the real composition: the identity stub yields fused order, no crash.
    recall = _make(
        episodic_query=lambda _q, _k: [
            _chunk("strong", distance=0.05, text="the answer"),
            _chunk("weak", distance=0.5, text="unrelated"),
        ],
        graph_retrieve=None,
    )
    out = recall("the answer")
    assert not out.abstained
    assert out.episodic[0].id == "strong"  # fused order under the stub


def test_no_owner_scope_reads_no_graph_but_still_recalls_episodic() -> None:
    # Fail-closed graph scope (K3 posture): no owner ⇒ no graph leg, episodic still works.
    recall = _make(owner_provider=lambda: None)
    out = recall("what is my address")
    assert out.graph.items == ()
    assert [c.id for c in out.episodic] == ["e1"]
