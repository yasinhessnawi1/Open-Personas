"""Unit tests for the K6 self-node primitives (pure — no DB).

The reserved id scheme (K6-D-5) and the ``NodeKind.SELF`` marker (K6-D-3) are the
contract the store's get-or-create depends on; the DB behaviour (idempotency,
rename, race) lives in ``tests/integration/test_graph_self_node_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona.graph.models import (
    ConceptNode,
    NodeKind,
    NodeProvenance,
    make_entity_id,
    make_node_id,
    make_self_node_id,
)
from persona.schema.chunks import WriteSource

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


def test_self_node_id_is_singular_and_indexless() -> None:
    # Exactly one self node per user → no monotonic index, unlike fact/entity ids.
    assert make_self_node_id("owner-1") == "owner-1::self"


def test_self_id_namespace_is_distinct_from_node_and_entity() -> None:
    owner = "owner-1"
    self_id = make_self_node_id(owner)
    assert "::node::" not in self_id
    assert "::entity::" not in self_id
    assert self_id != make_node_id(owner, 0)
    assert self_id != make_entity_id(owner, 0)


def test_self_id_is_deterministic_per_owner() -> None:
    # Determinism is what makes get-or-create idempotent by primary key (K6-D-5).
    assert make_self_node_id("u") == make_self_node_id("u")
    assert make_self_node_id("a") != make_self_node_id("b")


def test_nodekind_self_exists_and_is_a_distinct_value() -> None:
    assert NodeKind.SELF == "self"
    assert NodeKind.SELF in set(NodeKind)
    assert NodeKind.SELF not in {
        NodeKind.CONCEPT,
        NodeKind.FACT,
        NodeKind.PREFERENCE,
        NodeKind.TRAIT,
        NodeKind.GOAL,
        NodeKind.CIRCUMSTANCE,
        NodeKind.ENTITY,
    }


def test_concept_node_accepts_self_kind_and_reserved_id() -> None:
    node = ConceptNode(
        id=make_self_node_id("u"),
        node_kind=NodeKind.SELF,
        concept_name="Ada",
        content="Ada",
        provenance=(NodeProvenance(source=WriteSource.SYSTEM, written_at=NOW),),
        created_at=NOW,
    )
    assert node.node_kind is NodeKind.SELF
    assert node.id == "u::self"
    assert node.content_hash  # auto-computed at construction
