"""Integration tests for the Postgres graph transport (Spec K0, T3).

Self-contained: builds the three graph tables from the core-side
``graph_metadata`` view via ``create_all`` (RLS is the migration's job, tested in
T4). Reads ``DATABASE_URL`` with the same safety gate as the api fixtures (only a
disposable ``*_test`` DB, since the fixture drops+recreates the graph tables).
Deterministic unit vectors drive the cosine-ordering assertions — no embedder.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.graph._schema import EMBEDDING_DIM, graph_metadata
from persona.graph.errors import GraphIndexError
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    EntityAlias,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_edge_id,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _vec(i: int) -> list[float]:
    """A 384-d unit vector with 1.0 at position ``i`` (orthogonal for distinct i)."""
    v = [0.0] * EMBEDDING_DIM
    v[i % EMBEDDING_DIM] = 1.0
    return v


def _prov(**kw: object) -> NodeProvenance:
    base: dict[str, object] = {"source": WriteSource.PERSONA_SELF, "written_at": NOW}
    base.update(kw)
    return NodeProvenance(**base)  # type: ignore[arg-type]


def _node(node_id: str, concept: str, content: str, **kw: object) -> ConceptNode:
    base: dict[str, object] = {
        "id": node_id,
        "node_kind": NodeKind.FACT,
        "concept_name": concept,
        "content": content,
        "provenance": (_prov(),),
        "created_at": NOW,
    }
    base.update(kw)
    return ConceptNode(**base)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def _graph_engine() -> Iterator[Engine]:
    """Session engine with the ``vector`` extension created once.

    Creating the extension once (not per test) avoids the
    ``CREATE EXTENSION IF NOT EXISTS`` system-catalog race that fires under
    rapid reconnection. Same DATABASE_URL safety gate as the api fixtures.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set; skipping Postgres integration test")
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")
    from sqlalchemy.engine import make_url

    db_name = make_url(url).database or ""
    if os.environ.get("PERSONA_TEST_DB") != "1" and not db_name.endswith("_test"):
        pytest.skip(
            f"Refusing destructive graph fixture against {db_name!r}: it drops+recreates "
            f"graph tables. Use a '*_test' DB or set PERSONA_TEST_DB=1."
        )

    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError, OperationalError

    engine: Engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except IntegrityError:
        pass  # concurrent CREATE EXTENSION IF NOT EXISTS catalog race; the extension exists
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"Postgres unreachable at DATABASE_URL: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def backend(_graph_engine: Engine) -> Iterator[PostgresGraphBackend]:
    with _graph_engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    yield PostgresGraphBackend(engine=_graph_engine)
    with _graph_engine.begin() as conn:
        graph_metadata.drop_all(conn)


# ----- node CRUD -----------------------------------------------------------


def test_insert_returns_surrogate_and_get_node_round_trips(backend: PostgresGraphBackend) -> None:
    node = _node("u1::node::00000001", "coffee", "likes strong coffee", wellbeing_category=None)
    surrogate = backend.insert_node("u1", node, _vec(0))
    assert isinstance(surrogate, int)

    got = backend.get_node("u1", "u1::node::00000001")
    assert got is not None
    assert got.content == "likes strong coffee"
    assert got.content_hash == node.content_hash
    assert len(got.provenance) == 1
    assert got.provenance[0].source is WriteSource.PERSONA_SELF
    assert backend.surrogate_for("u1", node.id) == surrogate


def test_get_node_is_owner_scoped(backend: PostgresGraphBackend) -> None:
    backend.insert_node("u1", _node("u1::node::00000001", "x", "y"), _vec(0))
    assert backend.get_node("u2", "u1::node::00000001") is None


def test_update_node_extends_content_and_trail(backend: PostgresGraphBackend) -> None:
    backend.insert_node("u1", _node("u1::node::00000001", "job", "works at X"), _vec(0))
    extended = _node(
        "u1::node::00000001",
        "job",
        "works at X; promoted to lead",
        provenance=(_prov(reason="created"), _prov(reason="extended")),
    )
    surrogate = backend.update_node("u1", extended, _vec(1))
    assert surrogate is not None

    got = backend.get_node("u1", "u1::node::00000001")
    assert got is not None
    assert got.content == "works at X; promoted to lead"
    assert len(got.provenance) == 2
    assert got.provenance[1].reason == "extended"


def test_update_missing_node_returns_none(backend: PostgresGraphBackend) -> None:
    assert backend.update_node("u1", _node("u1::node::00000099", "x", "y"), _vec(0)) is None


def test_delete_node_returns_surrogate_and_removes(backend: PostgresGraphBackend) -> None:
    surrogate = backend.insert_node("u1", _node("u1::node::00000001", "x", "y"), _vec(0))
    assert backend.delete_node("u1", "u1::node::00000001") == surrogate
    assert backend.get_node("u1", "u1::node::00000001") is None
    assert backend.delete_node("u1", "u1::node::00000001") is None


def test_get_nodes_by_surrogates(backend: PostgresGraphBackend) -> None:
    s1 = backend.insert_node("u1", _node("u1::node::00000001", "a", "aa"), _vec(0))
    s2 = backend.insert_node("u1", _node("u1::node::00000002", "b", "bb"), _vec(1))
    got = backend.get_nodes_by_surrogates("u1", [s1, s2])
    assert {got[s1].id, got[s2].id} == {"u1::node::00000001", "u1::node::00000002"}
    assert backend.get_nodes_by_surrogates("u1", []) == {}


# ----- dense + FTS ---------------------------------------------------------


def test_dense_query_ranks_by_cosine(backend: PostgresGraphBackend) -> None:
    backend.insert_node("u1", _node("u1::node::00000001", "near", "near"), _vec(0))
    backend.insert_node("u1", _node("u1::node::00000002", "far", "far"), _vec(1))
    results = backend.dense_query("u1", _vec(0), top_k=2)
    assert results[0].id == "u1::node::00000001"
    assert results[0].distance is not None
    assert results[0].distance < results[1].distance


def test_dense_query_allowlist_restricts(backend: PostgresGraphBackend) -> None:
    s1 = backend.insert_node("u1", _node("u1::node::00000001", "a", "a"), _vec(0))
    backend.insert_node("u1", _node("u1::node::00000002", "b", "b"), _vec(1))
    results = backend.dense_query("u1", _vec(1), top_k=5, allowed_surrogates=[s1])
    assert [r.id for r in results] == ["u1::node::00000001"]


def test_dense_query_empty_allowlist_returns_nothing(backend: PostgresGraphBackend) -> None:
    backend.insert_node("u1", _node("u1::node::00000001", "a", "a"), _vec(0))
    assert backend.dense_query("u1", _vec(0), top_k=5, allowed_surrogates=[]) == []


def test_fts_query_finds_term(backend: PostgresGraphBackend) -> None:
    backend.insert_node("u1", _node("u1::node::00000001", "meds", "takes metformin daily"), _vec(0))
    backend.insert_node("u1", _node("u1::node::00000002", "diet", "is vegetarian"), _vec(1))
    results = backend.fts_query("u1", "metformin", top_k=5)
    assert [r.id for r in results] == ["u1::node::00000001"]


# ----- wellbeing-layer reads (K4) ------------------------------------------


def test_flagged_nodes_returns_only_wellbeing_tagged(backend: PostgresGraphBackend) -> None:
    backend.insert_node(
        "u1",
        _node("u1::node::00000001", "meds", "takes metformin", wellbeing_category="health"),
        _vec(0),
    )
    backend.insert_node("u1", _node("u1::node::00000002", "hobby", "likes chess"), _vec(1))
    backend.insert_node(
        "u2", _node("u2::node::00000001", "x", "y", wellbeing_category="health"), _vec(2)
    )
    flagged = backend.flagged_nodes("u1")
    assert [n.id for n in flagged] == ["u1::node::00000001"]  # only u1's tagged node
    assert flagged[0].wellbeing_category == "health"
    assert flagged[0].provenance  # full provenance trail hydrated (K4 recency)


def test_node_ids_for_owner_enumerates_all_owner_ids(backend: PostgresGraphBackend) -> None:
    backend.insert_node(
        "u1", _node("u1::node::00000001", "a", "a", wellbeing_category="x"), _vec(0)
    )
    backend.insert_node("u1", _node("u1::node::00000002", "b", "b"), _vec(1))
    backend.insert_node("u2", _node("u2::node::00000001", "c", "c"), _vec(2))
    assert sorted(backend.node_ids_for_owner("u1")) == [
        "u1::node::00000001",
        "u1::node::00000002",
    ]  # owner-scoped; flagged or not


# ----- embeddings ----------------------------------------------------------


def test_get_embeddings_and_iter_for_rebuild(backend: PostgresGraphBackend) -> None:
    s1 = backend.insert_node("u1", _node("u1::node::00000001", "a", "a"), _vec(0))
    backend.insert_node("u1", _node("u1::node::00000002", "b", "b"), _vec(3))
    embs = backend.get_embeddings("u1", ["u1::node::00000001"])
    assert embs["u1::node::00000001"][0] == 1.0
    rebuild = dict(backend.iter_embeddings("u1"))
    assert rebuild[s1][0] == 1.0
    assert len(rebuild) == 2


# ----- edges + traversal ---------------------------------------------------


def _edge(src: str, dst: str, lt: LinkType, owner_prov: bool = False) -> TypedLink:
    return TypedLink(
        id=make_edge_id(src, dst, lt),
        src_node_id=src,
        dst_node_id=dst,
        link_type=lt,
        weight=0.9,
        provenance=_prov() if owner_prov else None,
        created_at=NOW,
    )


def test_neighbors_both_directions_and_type_filter(backend: PostgresGraphBackend) -> None:
    a, b, c = "u1::node::00000001", "u1::node::00000002", "u1::node::00000003"
    for nid in (a, b, c):
        backend.insert_node("u1", _node(nid, nid, nid), _vec(int(nid[-1])))
    backend.upsert_edge("u1", _edge(a, b, LinkType.SEMANTIC))
    backend.upsert_edge("u1", _edge(c, a, LinkType.ENTITY))  # incoming to a

    all_n = backend.neighbors("u1", a, limit=10)
    assert {n.id for _, n in all_n} == {b, c}  # both directions

    only_entity = backend.neighbors("u1", a, link_types={LinkType.ENTITY}, limit=10)
    assert {n.id for _, n in only_entity} == {c}
    assert only_entity[0][0].link_type is LinkType.ENTITY


def test_upsert_edge_is_idempotent(backend: PostgresGraphBackend) -> None:
    a, b = "u1::node::00000001", "u1::node::00000002"
    backend.insert_node("u1", _node(a, "a", "a"), _vec(0))
    backend.insert_node("u1", _node(b, "b", "b"), _vec(1))
    backend.upsert_edge("u1", _edge(a, b, LinkType.SEMANTIC))
    backend.upsert_edge("u1", _edge(a, b, LinkType.SEMANTIC))  # same edge id → no dup
    assert len(backend.neighbors("u1", a, limit=10)) == 1


def test_deleting_node_cascades_its_edges(backend: PostgresGraphBackend) -> None:
    a, b = "u1::node::00000001", "u1::node::00000002"
    backend.insert_node("u1", _node(a, "a", "a"), _vec(0))
    backend.insert_node("u1", _node(b, "b", "b"), _vec(1))
    backend.upsert_edge("u1", _edge(a, b, LinkType.SEMANTIC))
    backend.delete_node("u1", a)
    assert backend.neighbors("u1", b, limit=10) == []


# ----- entities ------------------------------------------------------------


def test_entity_crud_and_alias(backend: PostgresGraphBackend) -> None:
    ent = CanonicalEntity(
        id="u1::entity::00000001",
        canonical_name="Dr. Hansen",
        aliases=(EntityAlias(surface="my doctor"),),
        provenance=_prov(),
        created_at=NOW,
    )
    backend.insert_entity("u1", ent, _vec(0))
    got = backend.get_entity("u1", ent.id)
    assert got is not None
    assert got.canonical_name == "Dr. Hansen"
    assert {a.surface for a in got.aliases} == {"my doctor"}

    backend.add_alias("u1", ent.id, EntityAlias(surface="the GP", confidence=0.8))
    got2 = backend.get_entity("u1", ent.id)
    assert got2 is not None
    assert {a.surface for a in got2.aliases} == {"my doctor", "the GP"}

    # R9-284 MEDIUM-2: the append is SQL; the same alias again is not repeated, and one that
    # differs in any field is appended.
    backend.add_alias("u1", ent.id, EntityAlias(surface="the GP", confidence=0.8))
    backend.add_alias("u1", ent.id, EntityAlias(surface="the GP", confidence=0.5))
    got3 = backend.get_entity("u1", ent.id)
    assert got3 is not None
    assert [(a.surface, a.confidence) for a in got3.aliases] == [
        ("my doctor", None),
        ("the GP", 0.8),
        ("the GP", 0.5),
    ]
    backend.add_alias("u1", "u1::entity::missing", EntityAlias(surface="x"))  # a no-op


def test_entity_candidates_ranks_by_cosine(backend: PostgresGraphBackend) -> None:
    near = CanonicalEntity(id="u1::entity::00000001", canonical_name="near", created_at=NOW)
    far = CanonicalEntity(id="u1::entity::00000002", canonical_name="far", created_at=NOW)
    backend.insert_entity("u1", near, _vec(0))
    backend.insert_entity("u1", far, _vec(1))
    cands = backend.entity_candidates("u1", _vec(0), top_k=2)
    assert cands[0][0].id == "u1::entity::00000001"
    assert cands[0][1] < cands[1][1]


# ----- node ↔ entity associations (T6b) -----------------------------------


def _seed_entity_and_two_nodes(backend: PostgresGraphBackend) -> str:
    a, b = "u1::node::00000001", "u1::node::00000002"
    backend.insert_node("u1", _node(a, "a", "a"), _vec(0))
    backend.insert_node("u1", _node(b, "b", "b"), _vec(1))
    ent = CanonicalEntity(id="u1::entity::00000001", canonical_name="Dr. Hansen", created_at=NOW)
    backend.insert_entity("u1", ent, _vec(2))
    return ent.id


def test_associate_entities_threads_nodes(backend: PostgresGraphBackend) -> None:
    eid = _seed_entity_and_two_nodes(backend)
    backend.associate_entities("u1", "u1::node::00000001", [eid])
    backend.associate_entities("u1", "u1::node::00000002", [eid])
    assert {n.id for n in backend.nodes_for_entity("u1", eid)} == {
        "u1::node::00000001",
        "u1::node::00000002",
    }
    assert backend.entities_for_node("u1", "u1::node::00000001") == [eid]
    assert {n.id for n in backend.entity_neighbors("u1", "u1::node::00000001")} == {
        "u1::node::00000002"
    }


def test_associate_entities_is_idempotent(backend: PostgresGraphBackend) -> None:
    eid = _seed_entity_and_two_nodes(backend)
    backend.associate_entities("u1", "u1::node::00000001", [eid])
    backend.associate_entities("u1", "u1::node::00000001", [eid])  # PK conflict → no-op
    assert len(backend.nodes_for_entity("u1", eid)) == 1


def test_deleting_node_cascades_associations(backend: PostgresGraphBackend) -> None:
    eid = _seed_entity_and_two_nodes(backend)
    backend.associate_entities("u1", "u1::node::00000001", [eid])
    backend.delete_node("u1", "u1::node::00000001")
    assert backend.nodes_for_entity("u1", eid) == []
    assert backend.entities_for_node("u1", "u1::node::00000001") == []


# ----- fail-fast -----------------------------------------------------------


def test_insert_node_rejects_wrong_embedding_dim(backend: PostgresGraphBackend) -> None:
    with pytest.raises(GraphIndexError, match="dimension mismatch"):
        backend.insert_node("u1", _node("u1::node::00000001", "x", "y"), [0.0, 1.0])
