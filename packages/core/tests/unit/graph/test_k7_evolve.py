"""Unit tests for the Spec K7 T2 evolve semantics (K7-D-1/-2/-7/-8/-9).

The supersede gate (event-time recency + trust-tier tie-break + not-superseding→
accumulate), evolve idempotency (UNCHANGED), the node-version preservation on a
supersede, the SELF + merged guards, and the allocator collision fix — all over the
in-memory ``_FakeBackend`` (the merge engine is transport-agnostic). DB behaviour is
covered by the integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona.graph.errors import GraphProtectedNodeError, NodeMergeError
from persona.graph.merge import MergeEngine, _supersedes
from persona.graph.models import NodeKind, NodeProvenance, make_self_node_id
from persona.graph.protocol import KnowledgeCandidate, MergeAction, UpdateIntent
from persona.schema.chunks import WriteSource

from .test_graph_merge import _FakeBackend, _MappingEmbedder, vec

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=30)
EARLIER = NOW - timedelta(days=30)


def _prov(source: WriteSource = WriteSource.PERSONA_SELF, **kw: object) -> NodeProvenance:
    base: dict[str, object] = {"source": source, "written_at": NOW}
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


# ----- the supersede predicate (K7-D-2) ------------------------------------


def test_supersedes_recency() -> None:
    assert _supersedes(LATER, WriteSource.PERSONA_SELF, NOW, WriteSource.SYSTEM)  # later wins
    assert not _supersedes(EARLIER, WriteSource.SYSTEM, NOW, WriteSource.PERSONA_SELF)  # earlier no


def test_supersedes_trust_tiebreak_on_equal_event_time() -> None:
    # Equal event time → higher-or-equal trust wins (system > user > persona_self).
    assert _supersedes(NOW, WriteSource.SYSTEM, NOW, WriteSource.PERSONA_SELF)
    assert _supersedes(NOW, WriteSource.USER, NOW, WriteSource.USER)  # equal trust ⇒ >=, wins
    assert not _supersedes(NOW, WriteSource.PERSONA_SELF, NOW, WriteSource.SYSTEM)  # lower loses


# ----- supersede path writes a version + reports EVOLVED (K7-D-1) ----------


def test_supersede_preserves_prior_content_and_embedding_in_a_version() -> None:
    b = _FakeBackend()
    mapping = {"lives in Bergen": vec(0), "lives in Oslo": vec(5)}
    eng = _engine(b, mapping)
    created = eng.merge("u1", _cand("lives in Bergen", provenance=_prov(written_at=NOW)))
    out = eng.merge(
        "u1",
        _cand(
            "lives in Oslo",
            update_intent=UpdateIntent.UPDATE,
            target_node_id=created.node_id,
            provenance=_prov(written_at=LATER),  # strictly later ⇒ supersedes
        ),
    )
    assert out.action is MergeAction.EVOLVED
    assert out.superseded_version_id == "1"
    # The version preserved the PRIOR content AND embedding byte-exact (§0).
    assert len(b.versions) == 1
    _key, node_id, prior_node, prior_emb, valid_at, invalid_at, _by = b.versions[0]
    assert node_id == created.node_id
    assert prior_node.content == "lives in Bergen"
    assert prior_emb == vec(0)
    assert valid_at == NOW  # prior account's world start (creation written_at)
    assert invalid_at == LATER  # the superseding fact's event time
    # current row now holds the new account + embedding.
    cur, cur_emb = b.nodes[created.node_id]
    assert cur.content == "lives in Oslo"
    assert cur_emb == vec(5)


def test_tie_won_by_trust_supersedes_with_nondegenerate_window() -> None:
    b = _FakeBackend()
    mapping = {"a": vec(0), "b": vec(5)}
    eng = _engine(b, mapping)
    # creation by persona_self at NOW; a user correction at the SAME event time wins.
    created = eng.merge(
        "u1", _cand("a", provenance=_prov(WriteSource.PERSONA_SELF, written_at=NOW))
    )
    out = eng.merge(
        "u1",
        _cand(
            "b",
            update_intent=UpdateIntent.CONTRADICT,
            target_node_id=created.node_id,
            provenance=_prov(WriteSource.USER, written_at=NOW),
        ),
    )
    assert out.action is MergeAction.EVOLVED
    _key, _nid, _node, _emb, valid_at, invalid_at, _by = b.versions[0]
    # nominal zero-width window bumped 1µs so the DDL CHECK holds, embedding preserved.
    assert invalid_at == valid_at + timedelta(microseconds=1)


# ----- not-superseding ⇒ accumulate, never destroy (K7-D-2) ----------------


def test_late_arriving_past_account_accumulates_not_supersedes() -> None:
    b = _FakeBackend()
    mapping = {"current job at Y": vec(0), "used to work at X": vec(0, 0.5, 1), "merged": vec(0)}
    eng = _engine(b, mapping)
    created = eng.merge("u1", _cand("current job at Y", provenance=_prov(written_at=NOW)))
    out = eng.merge(
        "u1",
        _cand(
            "used to work at X",
            update_intent=UpdateIntent.UPDATE,
            target_node_id=created.node_id,
            provenance=_prov(written_at=EARLIER),  # about the PAST ⇒ does not supersede
        ),
    )
    # Accumulate (extend semantics), no version closes, current account intact.
    assert out.action is MergeAction.EXTENDED
    assert b.versions == []
    node, _ = b.nodes[created.node_id]
    assert "current job at Y" in node.content
    assert "used to work at X" in node.content


# ----- evolve idempotency → UNCHANGED (K7-D-8) -----------------------------


def test_replaying_same_update_is_unchanged_noop() -> None:
    b = _FakeBackend()
    mapping = {"first": vec(0), "second": vec(5)}
    eng = _engine(b, mapping)
    created = eng.merge("u1", _cand("first"))
    prov = _prov(written_at=LATER, interaction_id="conv-9")
    first = eng.merge(
        "u1",
        _cand(
            "second",
            update_intent=UpdateIntent.UPDATE,
            target_node_id=created.node_id,
            provenance=prov,
        ),
    )
    assert first.action is MergeAction.EVOLVED
    # Replay the exact same candidate (same content + same interaction/source).
    replay = eng.merge(
        "u1",
        _cand(
            "second",
            update_intent=UpdateIntent.UPDATE,
            target_node_id=created.node_id,
            provenance=prov,
        ),
    )
    assert replay.action is MergeAction.UNCHANGED
    assert len(b.versions) == 1  # nothing new written on replay


# ----- SELF + merged guards (K7-D-7) ---------------------------------------


def test_evolve_on_self_raises_protected() -> None:
    b = _FakeBackend()
    eng = _engine(b, {"x": vec(0)})
    self_id = make_self_node_id("u1")
    with pytest.raises(GraphProtectedNodeError):
        eng.merge(
            "u1",
            _cand("x", update_intent=UpdateIntent.UPDATE, target_node_id=self_id),
        )


def test_evolve_on_merged_node_raises() -> None:
    b = _FakeBackend()
    mapping = {"orig": vec(0), "new": vec(5)}
    eng = _engine(b, mapping)
    created = eng.merge("u1", _cand("orig"))
    b.merged.add(created.node_id)  # consolidated into a canonical
    with pytest.raises(NodeMergeError):
        eng.merge(
            "u1",
            _cand("new", update_intent=UpdateIntent.UPDATE, target_node_id=created.node_id),
        )


def test_extend_target_excludes_self() -> None:
    # A SELF node in the index must never be chosen as an extend target (K7-D-7):
    # dense_query(exclude_self=True) drops it, so a near-identical fact creates a new
    # node rather than extending the anchor.
    from persona.graph.models import ConceptNode

    b = _FakeBackend()
    self_id = make_self_node_id("u1")
    b.nodes[self_id] = (
        ConceptNode(
            id=self_id,
            node_kind=NodeKind.SELF,
            concept_name="Alice",
            content="Alice",
            provenance=(_prov(WriteSource.SYSTEM),),
            created_at=NOW,
        ),
        vec(0),
    )
    eng = _engine(b, {"Alice profile": vec(0)})  # identical embedding to SELF
    out = eng.merge("u1", _cand("Alice profile"))
    assert out.action is MergeAction.CREATED  # created a new node, did NOT extend SELF
    assert out.node_id != self_id


# ----- allocator collision fix (K7-D-9) ------------------------------------


def test_allocator_no_collision_after_delete() -> None:
    b = _FakeBackend()
    mapping = {"n0": vec(0), "n1": vec(2), "n2": vec(4)}
    eng = _engine(b, mapping)
    a = eng.merge("u1", _cand("n0"))
    b2 = eng.merge("u1", _cand("n1"))
    assert a.node_id.endswith("00000000")
    assert b2.node_id.endswith("00000001")
    # delete the first, then create again — must NOT reuse index 1 (b2 is live).
    del b.nodes[a.node_id]
    c = eng.merge("u1", _cand("n2"))
    assert c.node_id != b2.node_id
    assert c.node_id.endswith("00000002")  # MAX(live index)+1, not count
