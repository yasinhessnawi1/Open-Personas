"""Unit tests for the merge engine — the coherence heart (Spec K0, T6a).

A fake transport stores ``(node, embedding)`` and computes REAL cosine in
``dense_query``, so extend-vs-create, accumulation, idempotency, and semantic-link
formation are exercised end-to-end in-memory. A mapping embedder assigns each
content a controlled vector so similarity is exact and deterministic.
"""

# ruff: noqa: ARG002 — fakes deliberately ignore some args
from __future__ import annotations

import inspect
import math
from datetime import UTC, datetime

import pytest
from persona.graph import merge as merge_module
from persona.graph.config import GraphSettings
from persona.graph.errors import NodeMergeError
from persona.graph.merge import MergeEngine, accumulate
from persona.graph.models import ConceptNode, LinkType, NodeKind, NodeProvenance, TypedLink
from persona.graph.protocol import KnowledgeCandidate, MergeAction, UpdateIntent
from persona.schema.chunks import WriteSource

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
DIM = 384


def vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    """A unit 384-d vector: ``cos`` along axis ``primary`` + the rest on ``secondary``."""
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0 or nb == 0 else dot / (na * nb)


class _MappingEmbedder:
    model_name = "mapping"

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self._mapping = mapping

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            if t in self._mapping:
                out.append(self._mapping[t])
                continue
            # Accumulated content ("a\nb") anchors to its longest known substring,
            # keeping an extended node near its cluster.
            keys = [k for k in self._mapping if k in t]
            if not keys:
                raise KeyError(t)
            out.append(self._mapping[max(keys, key=len)])
        return out


#: A stored version snapshot the fake keeps for assertions (K7-D-1).
_VersionRow = tuple[int, str, ConceptNode, list[float], datetime, datetime, "str | None"]


class _FakeBackend:
    def __init__(self) -> None:
        self.nodes: dict[str, tuple[ConceptNode, list[float]]] = {}
        self.edges: dict[str, TypedLink] = {}
        self.associations: list[tuple[str, str]] = []  # (node_id, entity_id)
        self.merged: set[str] = set()  # node ids marked merged_into (K7-D-4)
        self.closed: list[tuple[str, str | None]] = []  # (edge_id, invalidated_by) closures
        self.salience: dict[str, tuple[float, int]] = {}  # node_id -> (salience, epoch)
        self.epoch = 0
        # version snapshots (K7-D-1): (version_key, node_id, node, embedding,
        # valid_at, invalid_at, invalidated_by)
        self.versions: list[_VersionRow] = []
        self._next_surrogate = 0
        self._next_version_key = 0

    def dense_query(
        self,
        owner_id: str,
        query_vector: list[float],
        top_k: int,
        *,
        allowed_surrogates: object = None,
        exclude_self: bool = False,
    ) -> list[ConceptNode]:
        candidates = [
            (node, emb)
            for node, emb in self.nodes.values()
            if node.id not in self.merged and not (exclude_self and node.node_kind is NodeKind.SELF)
        ]
        ranked = sorted(candidates, key=lambda ne: -_cosine(query_vector, ne[1]))
        out = []
        for node, emb in ranked[:top_k]:
            out.append(node.model_copy(update={"distance": 1.0 - _cosine(query_vector, emb)}))
        return out

    def count_nodes(self, owner_id: str) -> int:
        return sum(1 for n, _ in self.nodes.values() if n.node_kind is not NodeKind.SELF)

    def next_node_index(self, owner_id: str) -> int:
        indices = [
            int(n.id.rsplit("::", 1)[-1])
            for n, _ in self.nodes.values()
            if n.node_kind is not NodeKind.SELF and n.id.rsplit("::", 1)[-1].isdigit()
        ]
        return max(indices, default=-1) + 1

    def is_merged(self, owner_id: str, node_id: str) -> bool:
        return node_id in self.merged

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        entry = self.nodes.get(node_id)
        return None if entry is None else entry[0]

    def insert_node(self, owner_id: str, node: ConceptNode, embedding: list[float]) -> int:
        self.nodes[node.id] = (node, list(embedding))
        self._next_surrogate += 1
        return self._next_surrogate

    def update_node(self, owner_id: str, node: ConceptNode, embedding: list[float]) -> int | None:
        if node.id not in self.nodes:
            return None
        self.nodes[node.id] = (node, list(embedding))
        return 1

    def snapshot_node_version(
        self,
        owner_id: str,
        node_id: str,
        *,
        valid_at: datetime,
        invalid_at: datetime,
        invalidated_by: str | None,
    ) -> int | None:
        entry = self.nodes.get(node_id)
        if entry is None:
            return None
        node, emb = entry
        self._next_version_key += 1
        self.versions.append(
            (self._next_version_key, node_id, node, list(emb), valid_at, invalid_at, invalidated_by)
        )
        return self._next_version_key

    def latest_version_invalid_at(self, owner_id: str, node_id: str) -> datetime | None:
        invalids = [v[5] for v in self.versions if v[1] == node_id]
        return max(invalids) if invalids else None

    def current_epoch(self, owner_id: str) -> int:
        return self.epoch

    def bump_salience(
        self, owner_id: str, node_id: str, *, delta: float, epoch: int, floor: float, cap: float
    ) -> None:
        sal, _ = self.salience.get(node_id, (1.0, 0))
        self.salience[node_id] = (max(floor, min(cap, sal + delta)), epoch)

    def upsert_edge(self, owner_id: str, link: TypedLink) -> None:
        self.edges[link.id] = link

    def delete_links_from(self, owner_id: str, node_id: str, link_type: LinkType) -> None:
        self.edges = {
            k: e
            for k, e in self.edges.items()
            if not (e.src_node_id == node_id and e.link_type is link_type)
        }

    def invalidate_edge(self, owner_id: str, edge_id: str, *, invalidated_by: str | None) -> bool:
        if edge_id in self.edges and edge_id not in {e for e, _ in self.closed}:
            self.closed.append((edge_id, invalidated_by))
            return True
        return False

    def associate_entities(self, owner_id: str, node_id: str, entity_ids: list[str]) -> None:
        for eid in entity_ids:
            if (node_id, eid) not in self.associations:
                self.associations.append((node_id, eid))


def _prov(reason: str | None = None) -> NodeProvenance:
    return NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW, reason=reason)


def _cand(content: str, *, name: str = "concept", **kw: object) -> KnowledgeCandidate:
    base: dict[str, object] = {
        "concept_name": name,
        "content": content,
        "node_kind": NodeKind.FACT,
        "provenance": _prov(),
    }
    base.update(kw)
    return KnowledgeCandidate(**base)  # type: ignore[arg-type]


def _engine(
    backend: _FakeBackend, mapping: dict[str, list[float]], **settings: float
) -> MergeEngine:
    cfg = GraphSettings(**settings) if settings else GraphSettings()  # type: ignore[arg-type]
    return MergeEngine(backend=backend, embedder=_MappingEmbedder(mapping), settings=cfg)


# ----- accumulate helper ---------------------------------------------------


def test_accumulate_appends_then_dedupes() -> None:
    assert accumulate("a", "b") == "a\nb"
    assert accumulate("a\nb", "b") == "a\nb"  # already present → no-op
    assert accumulate("", "a") == "a"


# ----- create vs extend ----------------------------------------------------


def test_first_merge_creates_a_node() -> None:
    b = _FakeBackend()
    out = _engine(b, {"likes coffee": vec(0)}).merge("u1", _cand("likes coffee"))
    assert out.action is MergeAction.CREATED
    assert len(b.nodes) == 1


def test_unrelated_knowledge_creates_a_second_node() -> None:
    b = _FakeBackend()
    mapping = {"likes coffee": vec(0), "enjoys hiking": vec(2)}  # cos 0 → create
    eng = _engine(b, mapping)
    eng.merge("u1", _cand("likes coffee"))
    out = eng.merge("u1", _cand("enjoys hiking"))
    assert out.action is MergeAction.CREATED
    assert len(b.nodes) == 2


def test_related_knowledge_extends_the_existing_node() -> None:
    b = _FakeBackend()
    mapping = {"likes coffee": vec(0), "loves espresso": vec(0, 0.95, 1)}  # cos 0.95 >= 0.88
    eng = _engine(b, mapping)
    created = eng.merge("u1", _cand("likes coffee"))
    out = eng.merge("u1", _cand("loves espresso"))
    assert out.action is MergeAction.EXTENDED
    assert out.node_id == created.node_id
    assert len(b.nodes) == 1
    node, _ = b.nodes[out.node_id]
    assert "likes coffee" in node.content
    assert "loves espresso" in node.content


def test_no_near_duplicate_accumulation_on_a_stream_of_related_facts() -> None:
    b = _FakeBackend()
    mapping = {
        "likes coffee": vec(0),
        "loves espresso": vec(0, 0.95, 1),
        "drinks a latte daily": vec(0, 0.93, 2),
    }
    eng = _engine(b, mapping)
    for text in mapping:
        eng.merge("u1", _cand(text))
    assert len(b.nodes) == 1  # one accumulating concept, not three near-duplicates
    node = next(iter(b.nodes.values()))[0]
    assert all(t in node.content for t in mapping)


def test_merge_threshold_is_config_driven() -> None:
    # Same 0.95-cos pair: a 0.99 bar forces CREATE; the default 0.88 bar EXTENDS.
    mapping = {"likes coffee": vec(0), "loves espresso": vec(0, 0.95, 1)}
    strict = _FakeBackend()
    e_strict = _engine(strict, mapping, merge_extend_threshold=0.99)
    e_strict.merge("u1", _cand("likes coffee"))
    assert e_strict.merge("u1", _cand("loves espresso")).action is MergeAction.CREATED
    assert len(strict.nodes) == 2


# ----- idempotency (K2 crit 8) --------------------------------------------


def test_re_merging_identical_candidate_is_a_noop() -> None:
    b = _FakeBackend()
    eng = _engine(b, {"likes coffee": vec(0)})
    eng.merge("u1", _cand("likes coffee"))
    node_before, _ = b.nodes["u1::node::00000000"]
    out = eng.merge("u1", _cand("likes coffee"))  # identical
    assert out.action is MergeAction.EXTENDED  # found the node…
    node_after, _ = b.nodes["u1::node::00000000"]
    assert len(b.nodes) == 1
    assert node_after.content == node_before.content  # …but added nothing
    assert len(node_after.provenance) == len(node_before.provenance)  # no trail growth


# ----- accumulate + provenance --------------------------------------------


def test_extend_appends_to_provenance_trail() -> None:
    b = _FakeBackend()
    mapping = {"likes coffee": vec(0), "loves espresso": vec(0, 0.95, 1)}
    eng = _engine(b, mapping)
    eng.merge("u1", _cand("likes coffee", provenance=_prov("created")))
    eng.merge("u1", _cand("loves espresso", provenance=_prov("extended")))
    node = next(iter(b.nodes.values()))[0]
    assert [p.reason for p in node.provenance] == ["created", "extended"]


# ----- update / contradiction (D-K0-4) ------------------------------------


def test_contradiction_replaces_content_and_records_prior_no_silent_overwrite() -> None:
    b = _FakeBackend()
    mapping = {"works at X": vec(0), "no longer works at X": vec(0)}
    eng = _engine(b, mapping)
    created = eng.merge("u1", _cand("works at X", provenance=_prov("initial")))
    out = eng.merge(
        "u1",
        _cand(
            "no longer works at X",
            update_intent=UpdateIntent.CONTRADICT,
            target_node_id=created.node_id,
            provenance=_prov("user correction"),
        ),
    )
    node, _ = b.nodes[out.node_id]
    assert node.content == "no longer works at X"  # current account wins
    assert len(node.provenance) == 2  # the change is recorded (not silent)
    # prior content preserved as STRUCTURED data (D-K0-4), not free-text
    assert node.provenance[-1].superseded_content == "works at X"
    # K7-D-1/-8: a supersede reports EVOLVED, points at the version, and the prior
    # account is preserved as a window-closed version row (nothing destroyed).
    assert out.action is MergeAction.EVOLVED
    assert out.superseded_version_id is not None
    assert len(b.versions) == 1
    assert b.versions[0][2].content == "works at X"  # the version snapshot's prior content


def test_update_without_target_raises() -> None:
    eng = _engine(_FakeBackend(), {"x": vec(0)})
    with pytest.raises(NodeMergeError, match="requires target_node_id"):
        eng.merge("u1", _cand("x", update_intent=UpdateIntent.UPDATE))


def test_update_with_missing_target_raises() -> None:
    eng = _engine(_FakeBackend(), {"x": vec(0)})
    with pytest.raises(NodeMergeError, match="target not found"):
        eng.merge("u1", _cand("x", update_intent=UpdateIntent.UPDATE, target_node_id="nope"))


# ----- auto semantic links (D-K0-2) ---------------------------------------


def test_semantic_link_forms_between_navigable_but_distinct_nodes() -> None:
    # cos 0.85: >= link bar (0.82) but < merge bar (0.88) → two nodes, one link.
    b = _FakeBackend()
    mapping = {"likes coffee": vec(0), "likes tea": vec(0, 0.85, 1)}
    eng = _engine(b, mapping)
    eng.merge("u1", _cand("likes coffee"))
    out = eng.merge("u1", _cand("likes tea"))
    assert out.action is MergeAction.CREATED
    assert len(b.nodes) == 2
    sem = [e for e in b.edges.values() if e.link_type is LinkType.SEMANTIC]
    assert any(e.src_node_id == out.node_id for e in sem)
    assert out.created_link_ids  # links reported


def test_semantic_links_respect_the_per_node_cap() -> None:
    b = _FakeBackend()
    # 5 distinct nodes, EACH linkable to "new" (cos 0.85 ≥ link bar, < merge bar) but
    # mutually dissimilar (cos 0.72 → separate, unlinked). With cap=2, "new" links to
    # only 2 of the 5 available neighbours.
    mapping = {f"n{i}": vec(0, 0.85, 1 + i) for i in range(5)}
    mapping["new"] = vec(0, 1.0)
    eng = _engine(b, mapping, max_semantic_links=2)
    for i in range(5):
        eng.merge("u1", _cand(f"n{i}", name=f"n{i}"))
    assert len(b.nodes) == 5  # all distinct
    out = eng.merge("u1", _cand("new", name="new"))
    sem_from_new = [
        e
        for e in b.edges.values()
        if e.src_node_id == out.node_id and e.link_type is LinkType.SEMANTIC
    ]
    assert len(sem_from_new) == 2  # capped, though 5 were eligible


def test_extend_re_evaluates_semantic_links() -> None:
    b = _FakeBackend()
    mapping = {
        "likes coffee": vec(0),
        "loves espresso": vec(0, 0.95, 1),
        "likes tea": vec(0, 0.85, 2),
    }
    eng = _engine(b, mapping)
    eng.merge("u1", _cand("likes tea"))  # a navigable neighbour
    created = eng.merge("u1", _cand("likes coffee"))
    # extend the coffee node; its semantic links are cleared + re-formed
    eng.merge("u1", _cand("loves espresso"))
    sem_from_coffee = [
        e
        for e in b.edges.values()
        if e.src_node_id == created.node_id and e.link_type is LinkType.SEMANTIC
    ]
    # exactly one outgoing semantic edge (to the tea node) — not duplicated by re-eval
    assert len({e.dst_node_id for e in sem_from_coffee}) == len(sem_from_coffee)


# ----- design-call #2: merge consumes resolved entity_ids, never resolves --


def test_merge_records_entity_associations_without_materialising_entity_edges() -> None:
    b = _FakeBackend()
    out = _engine(b, {"x": vec(0)}).merge("u1", _cand("x", entity_ids=("u1::entity::00000001",)))
    assert out.entity_ids == ("u1::entity::00000001",)  # echoed through
    # T6b: the association is RECORDED (join table)…
    assert (out.node_id, "u1::entity::00000001") in b.associations
    # …but NO node↔node ENTITY edge is materialised (on-the-fly traversal model).
    assert not [e for e in b.edges.values() if e.link_type is LinkType.ENTITY]


def test_merge_attaches_proposed_typed_links() -> None:
    from persona.graph.protocol import ProposedLink

    b = _FakeBackend()
    mapping = {"burnout": vec(0), "job change": vec(2)}  # unrelated → two nodes
    eng = _engine(b, mapping)
    a = eng.merge("u1", _cand("burnout"))
    out = eng.merge(
        "u1",
        _cand(
            "job change",
            proposed_links=(
                ProposedLink(target_node_id=a.node_id, link_type=LinkType.CAUSAL, reason="led to"),
            ),
        ),
    )
    causal = [e for e in b.edges.values() if e.link_type is LinkType.CAUSAL]
    assert len(causal) == 1
    assert causal[0].src_node_id == out.node_id
    assert causal[0].dst_node_id == a.node_id
    assert causal[0].id in out.created_link_ids


def test_merge_skips_self_targeting_proposed_link() -> None:
    from persona.graph.protocol import ProposedLink

    b = _FakeBackend()
    # A proposed link whose target is the node being created must be skipped (no self-loop).
    eng = _engine(b, {"x": vec(0)})
    # Pre-create so we know the id, then re-merge identical (extend no-op) with a self link.
    first = eng.merge("u1", _cand("x"))
    out = eng.merge(
        "u1",
        _cand(
            "x",
            proposed_links=(ProposedLink(target_node_id=first.node_id, link_type=LinkType.CAUSAL),),
        ),
    )
    assert out.node_id == first.node_id
    assert not [e for e in b.edges.values() if e.link_type is LinkType.CAUSAL]


def test_merge_engine_never_calls_resolve() -> None:
    src = inspect.getsource(merge_module)
    assert ".resolve(" not in src
    assert "EntityRegistry" not in src
