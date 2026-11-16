"""Unit tests for the GraphStore assembly (Spec K0, T8).

Fakes the merge engine, transport, and index so the store's own responsibilities
— same-path index sync, exactly-one-audit, owner_id→allowlist scoping, neighbors
dispatch, rebuild — are tested in isolation (the collaborators are tested
elsewhere).
"""

# ruff: noqa: ARG002 — fakes deliberately ignore some args
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.graph.errors import GraphIndexError, GraphProtectedNodeError
from persona.graph.models import (
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_self_node_id,
)
from persona.graph.protocol import (
    GraphStore,
    KnowledgeCandidate,
    MergeAction,
    MergeOutcome,
)
from persona.graph.store import PostgresGraphStore
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Sequence

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
DIM = 8


def _vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    return v


def _node(node_id: str, *, distance: float | None = None) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name="c",
        content="c",
        distance=distance,
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),),
        created_at=NOW,
    )


def _cand(content: str = "x", **kw: object) -> KnowledgeCandidate:
    base: dict[str, object] = {
        "concept_name": "c",
        "content": content,
        "node_kind": NodeKind.FACT,
        "provenance": NodeProvenance(
            source=WriteSource.PERSONA_SELF, written_at=NOW, persona_id="tutor"
        ),
    }
    base.update(kw)
    return KnowledgeCandidate(**base)  # type: ignore[arg-type]


class _FakeMerge:
    def __init__(self, outcome: MergeOutcome) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        self.calls.append(candidate.content)
        return self.outcome


def _created_merge(node_id: str = "n") -> _FakeMerge:
    return _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id=node_id))


class _FakeIndex:
    def __init__(self) -> None:
        self.added: list[tuple[int, list[float]]] = []
        self.replaced: list[tuple[int, list[float]]] = []
        self.removed: list[int] = []
        self.search_result: list[tuple[int, float]] = []
        self.last_allowlist: Sequence[int] | None = None
        self.fail_add = False

    def add(self, *, surrogate: int, vector: Sequence[float]) -> None:
        if self.fail_add:
            raise GraphIndexError("boom", context={})
        self.added.append((surrogate, list(vector)))

    def replace(self, *, surrogate: int, vector: Sequence[float]) -> None:
        self.replaced.append((surrogate, list(vector)))

    def remove(self, surrogate: int) -> bool:
        self.removed.append(surrogate)
        return True

    def contains(self, surrogate: int) -> bool:
        return True

    def search(
        self, *, query_vector: Sequence[float], top_k: int, allowlist: Sequence[int] | None = None
    ) -> list[tuple[int, float]]:
        self.last_allowlist = allowlist
        return self.search_result[:top_k]

    def rebuild(self, items: object) -> None: ...
    def persist(self) -> None: ...


class _FakeBackend:
    def __init__(self) -> None:
        self.nodes: dict[str, tuple[int, ConceptNode, list[float]]] = {}
        self.owner_surrogates: list[int] = []
        self.node_surrogates: dict[str, int] = {}
        self.fts_result: list[ConceptNode] = []
        self.flagged_result: list[ConceptNode] = []
        self.owner_node_ids: list[str] = []
        self.edge_neighbors: list[tuple[TypedLink, ConceptNode]] = []
        self.entity_neighbor_nodes: list[ConceptNode] = []
        self.deleted: list[str] = []
        self.delete_returns: int | None = 7

    def surrogate_for(self, owner_id: str, node_id: str) -> int | None:
        return self.nodes[node_id][0] if node_id in self.nodes else None

    def get_embeddings(self, owner_id: str, node_ids: Sequence[str]) -> dict[str, list[float]]:
        return {nid: self.nodes[nid][2] for nid in node_ids if nid in self.nodes}

    def get_nodes_by_surrogates(
        self, owner_id: str, surrogates: Sequence[int]
    ) -> dict[int, ConceptNode]:
        by_surr = {s: (n, e) for s, n, e in ((v[0], v[1], v[2]) for v in self.nodes.values())}
        return {s: by_surr[s][0] for s in surrogates if s in by_surr}

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        return self.nodes[node_id][1] if node_id in self.nodes else None

    def delete_node(self, owner_id: str, node_id: str) -> int | None:
        self.deleted.append(node_id)
        return self.delete_returns

    def surrogates_for_owner(self, owner_id: str) -> list[int]:
        return list(self.owner_surrogates)

    def surrogates_for_nodes(self, owner_id: str, node_ids: Sequence[str]) -> list[int]:
        return [self.node_surrogates[n] for n in node_ids if n in self.node_surrogates]

    def fts_query(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]:
        return self.fts_result[:top_k]

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        return list(self.flagged_result)

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        return list(self.owner_node_ids)

    def neighbors(
        self,
        owner_id: str,
        node_id: str,
        *,
        link_types: set[LinkType] | None,
        limit: int,
        as_of: datetime | None = None,
    ) -> list[tuple[TypedLink, ConceptNode]]:
        return self.edge_neighbors[:limit]

    def entity_neighbors(self, owner_id: str, node_id: str) -> list[ConceptNode]:
        return list(self.entity_neighbor_nodes)

    def iter_embeddings(self, owner_id: str) -> list[tuple[int, list[float]]]:
        return [(s, e) for s, _n, e in self.nodes.values()]

    def register(self, node_id: str, surrogate: int, vec: list[float]) -> None:
        self.nodes[node_id] = (surrogate, _node(node_id), vec)
        self.node_surrogates[node_id] = surrogate
        self.owner_surrogates.append(surrogate)


def _store(
    backend: _FakeBackend, index: _FakeIndex, merge: _FakeMerge, audit: MemoryAuditLogger
) -> PostgresGraphStore:
    class _Emb:
        model_name = "fake"

        @property
        def dimension(self) -> int:
            return DIM

        def encode(self, texts: list[str]) -> list[list[float]]:
            return [_vec(0) for _ in texts]

    return PostgresGraphStore(
        backend=backend, index=index, merge_engine=merge, embedder=_Emb(), audit_logger=audit
    )


# ----- protocol ------------------------------------------------------------


def test_store_satisfies_graph_store_protocol() -> None:
    s = _store(_FakeBackend(), _FakeIndex(), _created_merge(), MemoryAuditLogger())
    assert isinstance(s, GraphStore)


# ----- same-path index sync + audit (criterion 8 + Spec 01) ----------------


def test_merge_created_adds_to_index_and_emits_one_audit() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    b.register("u1::node::00000001", 42, _vec(1))
    merge = _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id="u1::node::00000001"))
    out = _store(b, idx, merge, audit).merge("u1", _cand())
    assert out.action is MergeAction.CREATED
    assert idx.added == [(42, _vec(1))]  # synced to index in the same call
    assert not idx.replaced
    assert len(audit.events) == 1
    ev = audit.events[0]
    assert ev.action is AuditAction.WRITE
    assert ev.store == "knowledge_graph"
    assert ev.persona_id == "u1"
    assert ev.chunk_ids == ["u1::node::00000001"]
    assert ev.written_by == "tutor"


def test_merge_extended_replaces_in_index() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    b.register("u1::node::00000001", 42, _vec(1))
    merge = _FakeMerge(MergeOutcome(action=MergeAction.EXTENDED, node_id="u1::node::00000001"))
    _store(b, idx, merge, audit).merge("u1", _cand())
    assert idx.replaced == [(42, _vec(1))]
    assert not idx.added


def test_merge_index_failure_raises_after_postgres_and_audit() -> None:
    # Postgres-first: the mutation + its audit stand; the index drift is surfaced
    # (raised), recoverable via rebuild — not a silent inconsistency.
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    b.register("u1::node::00000001", 42, _vec(1))
    idx.fail_add = True
    merge = _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id="u1::node::00000001"))
    with pytest.raises(GraphIndexError):
        _store(b, idx, merge, audit).merge("u1", _cand())
    assert len(audit.events) == 1  # the authoritative mutation was still audited


def test_delete_removes_from_both_and_audits_once() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    store = _store(b, idx, _created_merge(), audit)
    assert store.delete_node("u1", "u1::node::00000001") is True
    assert b.deleted == ["u1::node::00000001"]
    assert idx.removed == [7]  # delete_returns
    assert len(audit.events) == 1
    assert audit.events[0].action is AuditAction.DELETE


def test_delete_self_node_raises_protected() -> None:
    # The SELF anchor is not deletable (K7-D-7): the guard fires before the backend,
    # so nothing is deleted and no audit is emitted.
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    store = _store(b, idx, _created_merge(), audit)
    with pytest.raises(GraphProtectedNodeError):
        store.delete_node("u1", make_self_node_id("u1"))
    assert b.deleted == []
    assert idx.removed == []
    assert audit.events == []


def test_delete_missing_node_is_false_and_no_audit() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    b.delete_returns = None
    s = _store(b, idx, _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id="n")), audit)
    assert s.delete_node("u1", "nope") is False
    assert idx.removed == []
    assert audit.events == []


def test_reads_emit_no_audit() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    s = _store(b, idx, _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id="n")), audit)
    s.search_dense("u1", "q", 5)
    s.search_fts("u1", "q", 5)
    s.neighbors("u1", "n", limit=5)
    s.get_node("u1", "n")
    assert audit.events == []


# ----- allowlist scoping from owner_id (criterion 6) -----------------------


def test_search_dense_passes_owner_surrogates_never_none() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    b.owner_surrogates = [1, 2, 3]
    s = _store(b, idx, _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id="n")), audit)
    s.search_dense("u1", "q", 5)  # allowlist=None → the user's surrogate set
    assert idx.last_allowlist == [1, 2, 3]  # NOT None — isolation never relies on None


def test_search_dense_k4_subtraction_maps_to_surrogates() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    b.node_surrogates = {"u1::node::00000001": 11, "u1::node::00000002": 22}
    s = _store(b, idx, _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id="n")), audit)
    s.search_dense("u1", "q", 5, allowlist={"u1::node::00000001"})
    assert idx.last_allowlist == [11]


def test_search_dense_hydrates_in_index_order_with_distance() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    b.register("u1::node::00000001", 11, _vec(0))
    b.register("u1::node::00000002", 22, _vec(1))
    idx.search_result = [(22, 0.9), (11, 0.8)]  # index order: 22 then 11
    s = _store(b, idx, _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id="n")), audit)
    out = s.search_dense("u1", "q", 5)
    assert [n.id for n in out] == ["u1::node::00000002", "u1::node::00000001"]
    assert out[0].distance == pytest.approx(0.1)  # 1 - 0.9


# ----- neighbors dispatch (edge types + on-the-fly ENTITY) -----------------


def test_neighbors_dispatches_edges_and_synthesises_entity_links() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    sem_edge = TypedLink(
        id="e", src_node_id="n", dst_node_id="m", link_type=LinkType.SEMANTIC, created_at=NOW
    )
    b.edge_neighbors = [(sem_edge, _node("m"))]
    b.entity_neighbor_nodes = [_node("k")]
    s = _store(b, idx, _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id="n")), audit)
    out = s.neighbors("u1", "n", limit=10)
    by_type = {edge.link_type: node.id for edge, node in out}
    assert by_type[LinkType.SEMANTIC] == "m"
    assert by_type[LinkType.ENTITY] == "k"  # synthesised on-the-fly from associations


def test_neighbors_entity_only_skips_edge_query() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    b.entity_neighbor_nodes = [_node("k")]
    s = _store(b, idx, _FakeMerge(MergeOutcome(action=MergeAction.CREATED, node_id="n")), audit)
    out = s.neighbors("u1", "n", link_types={LinkType.ENTITY}, limit=10)
    assert [n.id for _, n in out] == ["k"]
    assert all(e.link_type is LinkType.ENTITY for e, _ in out)


# ----- rebuild (criterion 9 / cold-start) ----------------------------------


def test_rebuild_index_resyncs_owner_vectors() -> None:
    b, idx, audit = _FakeBackend(), _FakeIndex(), MemoryAuditLogger()
    b.register("u1::node::00000001", 11, _vec(0))
    b.register("u1::node::00000002", 22, _vec(1))
    _store(b, idx, _created_merge(), audit).rebuild_index("u1")
    assert sorted(idx.removed) == [11, 22]  # cleared first
    assert sorted(s for s, _ in idx.added) == [11, 22]  # re-added from Postgres


def test_a_self_rename_that_lost_the_race_is_redone_on_a_fresh_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-R9-284-node-cas: the rename re-reads the SELF node and tries again."""
    from persona.graph.errors import GraphStaleWriteError  # noqa: PLC0415

    backend = _FakeBackend()
    self_node = ConceptNode(
        id="u1::self",
        node_kind=NodeKind.SELF,
        concept_name="Old",
        content="Old",
        provenance=(NodeProvenance(source=WriteSource.USER, written_at=NOW),),
        created_at=NOW,
    )
    backend.nodes[self_node.id] = (1, self_node, _vec(0))
    tries: list[str] = []

    racer = NodeProvenance(source=WriteSource.SYSTEM, written_at=NOW, reason="concurrent")
    raced = self_node.model_copy(
        update={
            "content": "Old, as another writer left it",
            "provenance": (*self_node.provenance, racer),
        }
    )
    raced = ConceptNode.model_validate(raced.model_dump(exclude={"content_hash"}))

    def _update(_owner: str, node: ConceptNode, _vector: object, **kw: str) -> int:
        tries.append(kw["expected_content_hash"])
        if len(tries) == 1:  # another writer changed the SELF node meanwhile
            backend.nodes[node.id] = (1, raced, _vec(0))
            raise GraphStaleWriteError("changed", context={"node_id": node.id})
        if kw["expected_content_hash"] != backend.nodes[node.id][1].content_hash:
            raise GraphStaleWriteError("changed", context={"node_id": node.id})
        backend.nodes[node.id] = (1, node, _vec(0))
        return 1

    backend.update_node = _update  # type: ignore[attr-defined]
    store = _store(backend, _FakeIndex(), _created_merge(), MemoryAuditLogger())
    monkeypatch.setattr(store, "_ensure_self_entity", lambda *_a, **_k: None)

    renamed = store._rename_self_node("u1", self_node, "New")  # noqa: SLF001

    assert raced.content_hash != self_node.content_hash, "the fresh read differs from the stale"
    assert renamed.concept_name == "New"
    assert tries == [self_node.content_hash, raced.content_hash], "read afresh, then written"
    assert racer in renamed.provenance, "the other writer's entry is kept"


def test_a_restore_that_lost_the_race_is_redone_on_a_fresh_read() -> None:
    """Round 11 LOW-4 (M11): the restore re-reads the node and tries again (node CAS)."""
    from persona.graph.errors import GraphStaleWriteError  # noqa: PLC0415

    backend = _FakeBackend()
    node_id = "u1::node::00000001"
    backend.register(node_id, 11, _vec(0))
    stale = backend.nodes[node_id][1]
    racer = NodeProvenance(source=WriteSource.SYSTEM, written_at=NOW, reason="concurrent")
    raced = ConceptNode.model_validate(
        stale.model_copy(
            update={"content": "changed meanwhile", "provenance": (*stale.provenance, racer)}
        ).model_dump(exclude={"content_hash"})
    )
    version = stale.model_copy(update={"content": "the old account"})
    backend.read_version_for_restore = lambda *_a: (version, _vec(1))  # type: ignore[attr-defined]
    tries: list[str] = []

    def _update(_owner: str, node: ConceptNode, _vector: object, **kw: str) -> int:
        tries.append(kw["expected_content_hash"])
        if len(tries) == 1:  # another writer changed the node meanwhile
            backend.nodes[node.id] = (11, raced, _vec(0))
            raise GraphStaleWriteError("changed", context={"node_id": node.id})
        if kw["expected_content_hash"] != backend.nodes[node.id][1].content_hash:
            raise GraphStaleWriteError("changed", context={"node_id": node.id})
        backend.nodes[node.id] = (11, node, _vec(1))
        return 11

    backend.update_node = _update  # type: ignore[attr-defined]
    store = _store(backend, _FakeIndex(), _created_merge(), MemoryAuditLogger())

    restored = store.restore_node_version("u1", node_id, "7")

    assert restored.content == "the old account"
    assert tries == [stale.content_hash, raced.content_hash], "read afresh, then written"
    assert racer in restored.provenance, "the other writer's entry is kept"
    assert restored.provenance[-1].superseded_content == "changed meanwhile"
