"""Erasing a persona takes it out of the user's graph, and leaves the rest (R9-284, ruling 4).

On a migrated Postgres, through the real graph backend: a node only the persona contributed to
goes with its edges, versions and entity links; a shared node keeps the other contributions;
the SELF node stays as dated stubs; an edge the persona asserted goes; an entity it established
goes when nothing links to it and otherwise loses the attribution. Runs in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_self_node_id,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource
from persona_api.services.erasure_graph import erase_persona_graph
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_alice"
_PERSONA = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_OTHER = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_SELF = make_self_node_id(_OWNER)
_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _by(persona_id: str | None) -> NodeProvenance:
    return NodeProvenance(
        source=WriteSource.SYSTEM if persona_id else WriteSource.USER,
        persona_id=persona_id,
        written_at=_NOW,
        grounding=f"what {persona_id} heard" if persona_id else None,
    )


def _node(node_id: str, *by: str | None, kind: NodeKind = NodeKind.FACT) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=kind,
        concept_name=node_id,
        content=f"about {node_id}",
        provenance=tuple(_by(p) for p in by),
        created_at=_NOW,
    )


def _edge(edge_id: str, src: str, dst: str, by: str) -> TypedLink:
    return TypedLink(
        id=edge_id,
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.CAUSAL,
        provenance=_by(by),
        created_at=_NOW,
        valid_at=_NOW,
    )


@pytest.fixture
def graph(migrated_engine: Engine, embedder: Any) -> Engine:  # noqa: ANN401
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        for pid in (_PERSONA, _OTHER):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine)
    vector = embedder.encode(["x"])[0]
    for node in (
        _node("only", _PERSONA, _PERSONA),
        _node("shared", _PERSONA, _OTHER),
        _node("theirs", _OTHER),
        _node("edited", _PERSONA),
        _node(_SELF, _PERSONA, kind=NodeKind.SELF),
    ):
        backend.insert_node(_OWNER, node, vector)
    for node_id in ("only", "shared", "edited", _SELF):
        backend.snapshot_node_version(
            _OWNER, node_id, valid_at=_NOW, invalid_at=_NOW, invalidated_by=None
        )
    # The user then edits a node only the persona had written: it is the user's now, and its
    # earlier version (the persona's alone) is not.
    backend.update_node(_OWNER, _node("edited", _PERSONA, None), vector)
    backend.upsert_edge(_OWNER, _edge("e_only", "only", "theirs", _OTHER))
    backend.upsert_edge(_OWNER, _edge("e_mine", "shared", "theirs", _PERSONA))
    backend.upsert_edge(_OWNER, _edge("e_theirs", "shared", "theirs", _OTHER))
    for entity_id, by in (("ent_gone", _PERSONA), ("ent_kept", _PERSONA), ("ent_theirs", _OTHER)):
        backend.insert_entity(
            _OWNER,
            CanonicalEntity(
                id=entity_id, canonical_name=entity_id, provenance=_by(by), created_at=_NOW
            ),
            vector,
        )
    with migrated_engine.begin() as conn:
        for node_id, entity_id in (
            ("only", "ent_gone"),
            ("theirs", "ent_kept"),
            ("theirs", "ent_theirs"),
        ):
            conn.execute(
                text(
                    "INSERT INTO graph_node_entities (owner_id, node_id, entity_id) "
                    "VALUES (:o, :n, :e)"
                ),
                {"o": _OWNER, "n": node_id, "e": entity_id},
            )
    return migrated_engine


def _column(engine: Engine, sql: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return {str(k): v for k, v in conn.execute(text(sql), {"o": _OWNER}).all()}


def _personas_in(trail: Any) -> list[str | None]:  # noqa: ANN401
    return [entry.get("persona_id") for entry in trail]


def test_the_persona_leaves_the_graph_and_everything_else_stays(graph: Engine) -> None:
    counts = erase_persona_graph(graph, owner_id=_OWNER, persona_id=_PERSONA)

    nodes = _column(graph, "SELECT id, provenance FROM graph_nodes WHERE owner_id = :o")
    assert set(nodes) == {"shared", "theirs", "edited", _SELF}
    assert _personas_in(nodes["shared"]) == [_OTHER]
    assert _personas_in(nodes["theirs"]) == [_OTHER]
    assert _personas_in(nodes["edited"]) == [None]
    assert nodes[_SELF] == [{"source": "system", "written_at": nodes[_SELF][0]["written_at"]}]

    versions = _column(
        graph, "SELECT node_id, provenance FROM graph_node_versions WHERE owner_id = :o"
    )
    assert set(versions) == {"shared", _SELF}
    assert _personas_in(versions["shared"]) == [_OTHER]
    assert _PERSONA not in str(versions[_SELF])

    edges = _column(graph, "SELECT id, 1 FROM graph_edges WHERE owner_id = :o")
    assert set(edges) == {"e_theirs"}

    entities = _column(graph, "SELECT id, provenance FROM graph_entities WHERE owner_id = :o")
    assert set(entities) == {"ent_kept", "ent_theirs"}
    assert entities["ent_kept"] is None
    assert entities["ent_theirs"]["persona_id"] == _OTHER

    assert counts == {
        "nodes_deleted": 1,
        "nodes_stripped": 3,
        "versions_deleted": 1,
        "versions_stripped": 2,
        "edges_deleted": 1,
        "entities_deleted": 1,
        "entities_unattributed": 1,
    }


def test_running_it_again_finds_nothing(graph: Engine) -> None:
    erase_persona_graph(graph, owner_id=_OWNER, persona_id=_PERSONA)

    again = erase_persona_graph(graph, owner_id=_OWNER, persona_id=_PERSONA)

    assert set(again.values()) == {0}


def test_another_owners_graph_is_not_reached(graph: Engine) -> None:
    """The persona id names one owner's persona; every statement is owner scoped as well."""
    counts = erase_persona_graph(graph, owner_id="user_bob", persona_id=_PERSONA)

    assert set(counts.values()) == {0}
    assert len(_column(graph, "SELECT id, 1 FROM graph_nodes WHERE owner_id = :o")) == 5
