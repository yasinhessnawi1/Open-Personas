"""Unit tests for the Spec K7 T3 edge-window semantics (K7-D-1/-1.3/-1.4/-8).

The ``close_link_ids`` K2 consumer-contract slot (validate-then-invalidate), and the
K7-D-1.3 structural guard: NO lifecycle path (evolve / extend / create / semantic
re-eval) auto-closes a temporal/causal assertion edge — only an explicit
``invalidate_edge`` (via ``close_link_ids`` or, later, consolidation) does. The full
window cycle (close → re-assert → two coexisting rows) + point-in-time ``neighbors``
are proven over real SQL in the integration suite.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

from persona.graph.merge import MergeEngine
from persona.graph.models import LinkType, NodeKind, NodeProvenance, TypedLink, make_edge_id
from persona.graph.protocol import KnowledgeCandidate, UpdateIntent
from persona.schema.chunks import WriteSource

from .test_graph_merge import _FakeBackend, _MappingEmbedder, vec

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _prov(**kw: object) -> NodeProvenance:
    base: dict[str, object] = {"source": WriteSource.PERSONA_SELF, "written_at": NOW}
    base.update(kw)
    return NodeProvenance(**base)  # type: ignore[arg-type]


def _engine(backend: _FakeBackend, mapping: dict[str, list[float]]) -> MergeEngine:
    from persona.graph.config import GraphSettings

    return MergeEngine(
        backend=backend, embedder=_MappingEmbedder(mapping), settings=GraphSettings()
    )


def _cand(content: str, **kw: object) -> KnowledgeCandidate:
    base: dict[str, object] = {
        "concept_name": "c",
        "content": content,
        "node_kind": NodeKind.FACT,
        "provenance": _prov(),
    }
    base.update(kw)
    return KnowledgeCandidate(**base)  # type: ignore[arg-type]


# ----- close_link_ids contract slot (K7-D-1.4 / K7-D-8) --------------------


def test_close_link_ids_invalidates_each_named_edge() -> None:
    b = _FakeBackend()
    eng = _engine(b, {"a fact": vec(0)})
    # seed two assertion edges the candidate will name to close.
    b.edges["e1"] = TypedLink(
        id="e1", src_node_id="x", dst_node_id="y", link_type=LinkType.TEMPORAL, created_at=NOW
    )
    b.edges["e2"] = TypedLink(
        id="e2", src_node_id="x", dst_node_id="z", link_type=LinkType.CAUSAL, created_at=NOW
    )
    eng.merge(
        "u1",
        _cand("a fact", close_link_ids=("e1", "e2"), provenance=_prov(interaction_id="conv-7")),
    )
    closed_ids = {e for e, _ in b.closed}
    assert closed_ids == {"e1", "e2"}
    # provenance-of-closure carries the interaction ref.
    assert all(by == "close:conv-7" for _, by in b.closed)


def test_no_close_link_ids_closes_nothing() -> None:
    b = _FakeBackend()
    eng = _engine(b, {"a fact": vec(0)})
    eng.merge("u1", _cand("a fact"))  # default close_link_ids == ()
    assert b.closed == []


# ----- K7-D-1.3 structural guard: no auto-close of assertion edges ----------


def test_supersede_does_not_autoclose_temporal_or_causal_edges() -> None:
    # A contradiction that SUPERSEDES the node must NOT close the node's
    # temporal/causal assertion edges (K7-D-1.3: historical assertions stay true when
    # current state changes; the node-version window IS the contradiction close).
    b = _FakeBackend()
    mapping = {"works at X": vec(0), "left X": vec(0)}
    eng = _engine(b, mapping)
    created = eng.merge("u1", _cand("works at X"))
    temporal = make_edge_id(created.node_id, "u1::node::00000099", LinkType.TEMPORAL)
    causal = make_edge_id(created.node_id, "u1::node::00000098", LinkType.CAUSAL)
    b.edges[temporal] = TypedLink(
        id=temporal,
        src_node_id=created.node_id,
        dst_node_id="u1::node::00000099",
        link_type=LinkType.TEMPORAL,
        created_at=NOW,
    )
    b.edges[causal] = TypedLink(
        id=causal,
        src_node_id=created.node_id,
        dst_node_id="u1::node::00000098",
        link_type=LinkType.CAUSAL,
        created_at=NOW,
    )
    eng.merge(
        "u1",
        _cand("left X", update_intent=UpdateIntent.CONTRADICT, target_node_id=created.node_id),
    )
    # the version window recorded the supersede; the assertion edges are untouched.
    assert b.versions  # a version WAS written (the supersede close)
    assert b.closed == []  # …but NO edge was invalidated
    assert temporal in b.edges
    assert causal in b.edges


def test_lifecycle_source_never_reaches_edge_close_for_assertions() -> None:
    # Structural belt-and-suspenders: the evolve/extend/create paths contain no
    # ``invalidate_edge`` call — the ONLY caller is ``_close_named_links`` (the
    # explicit K2 slot). Semantic re-eval is the only edge deletion, and it is
    # SEMANTIC-scoped (derived wiring, K7-D-1.3).
    for method in (MergeEngine._evolve, MergeEngine._extend, MergeEngine._create):
        assert "invalidate_edge" not in inspect.getsource(method)
    close_src = inspect.getsource(MergeEngine._close_named_links)
    assert "invalidate_edge" in close_src  # the sole caller
    sem_src = inspect.getsource(MergeEngine._form_semantic_links)
    assert "LinkType.SEMANTIC" in sem_src
    assert "delete_links_from" in sem_src
