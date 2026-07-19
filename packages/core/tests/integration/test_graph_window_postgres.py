"""Integration tests for the K5 windowed-read primitives (Spec K5, Group A).

Against real Postgres: ``seed_nodes`` (degree DESC, then recency), ``edges_among``
(the induced sub-graph of a node set), and ``count_nodes`` — the reads the Memory
window projects. Deterministic: nodes + edges are inserted directly via the backend
so degrees are exact (no reliance on auto-link thresholds).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.graph._schema import graph_metadata
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

DIM = 384
NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _node(node_id: str, *, created_at: datetime) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"content for {node_id}",
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),),
        created_at=created_at,
    )


def _edge(src: str, dst: str) -> TypedLink:
    return TypedLink(
        id=f"{src}->{dst}",
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.SEMANTIC,
        created_at=NOW,
    )


@pytest.fixture(scope="session")
def _engine() -> Iterator[Engine]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set; skipping Postgres integration test")
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")
    from sqlalchemy.engine import make_url

    db_name = make_url(url).database or ""
    if os.environ.get("PERSONA_TEST_DB") != "1" and not db_name.endswith("_test"):
        pytest.skip("Use a '*_test' DB or set PERSONA_TEST_DB=1 (destructive fixture).")
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError, OperationalError

    engine: Engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except IntegrityError:
        pass
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"Postgres unreachable: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def backend(_engine: Engine) -> Iterator[PostgresGraphBackend]:
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    yield PostgresGraphBackend(engine=_engine)
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)


def _seed_star(backend: PostgresGraphBackend, owner: str) -> None:
    """A hub linked to three leaves + one isolated node, with DISTINCT ``created_at`` so the
    recency seed order is unambiguous: lonely (NOW+1h) > leaf3 > leaf2 > leaf1 > hub (NOW).
    The edges exist so ``edges_among`` has structure to return; they no longer affect order.
    """
    vec = [0.0] * DIM
    backend.insert_node(owner, _node("hub", created_at=NOW), vec)
    for i in range(1, 4):
        backend.insert_node(owner, _node(f"leaf{i}", created_at=NOW + timedelta(minutes=i)), vec)
        backend.upsert_edge(owner, _edge("hub", f"leaf{i}"))
    backend.insert_node(owner, _node("lonely", created_at=NOW + timedelta(hours=1)), vec)


def test_count_nodes_counts_only_the_owner(backend: PostgresGraphBackend) -> None:
    _seed_star(backend, "u1")
    backend.insert_node("u2", _node("other", created_at=NOW), [0.0] * DIM)
    assert backend.count_nodes("u1") == 5  # noqa: PLR2004 — hub + 3 leaves + lonely
    assert backend.count_nodes("u2") == 1


def test_seed_nodes_orders_by_recency(backend: PostgresGraphBackend) -> None:
    """K5-D-8 (B1-refined): the seed is most-recent-first (``created_at`` DESC) — index-served."""
    _seed_star(backend, "u1")
    ids = [n.id for n in backend.seed_nodes("u1", limit=10)]
    assert ids[0] == "lonely"  # the newest node (NOW + 1h)
    assert ids[-1] == "hub"  # the oldest (NOW) — degree no longer influences the seed order
    assert ids.index("leaf3") < ids.index("leaf1")  # newer leaf precedes the older


def test_seed_nodes_respects_limit(backend: PostgresGraphBackend) -> None:
    _seed_star(backend, "u1")
    assert backend.seed_nodes("u1", limit=2) == backend.seed_nodes("u1", limit=10)[:2]
    assert backend.seed_nodes("u1", limit=0) == []


def test_seed_nodes_excludes_consolidated_away_nodes(backend: PostgresGraphBackend) -> None:
    """A consolidated-away node (``merged_into`` set) must NOT appear in the seed window.

    Every other read path (retrieval, ``neighbors``, ``recent_nodes``, the pgvector index)
    already filters ``merged_into IS NULL`` (K7-D-4); ``seed_nodes`` — the first-paint graph
    window — historically forgot it, so the UI drew consolidated duplicates next to their
    canonical node (Spec K12 thread E).
    """
    from sqlalchemy import text

    _seed_star(backend, "u1")
    # Consolidation merged leaf1 into hub.
    with backend._engine.begin() as conn:  # noqa: SLF001 — test reaches into the backend engine
        conn.execute(
            text("UPDATE graph_nodes SET merged_into = :c WHERE id = :m AND owner_id = 'u1'"),
            {"c": "hub", "m": "leaf1"},
        )
    ids = [n.id for n in backend.seed_nodes("u1", limit=10)]
    assert "leaf1" not in ids  # the consolidated-away node is hidden
    assert "hub" in ids  # its canonical remains
    assert set(ids) == {"lonely", "leaf3", "leaf2", "hub"}  # exactly the 4 live nodes


def test_edges_among_returns_only_induced_edges(backend: PostgresGraphBackend) -> None:
    _seed_star(backend, "u1")
    full = backend.edges_among("u1", ["hub", "leaf1", "leaf2", "leaf3", "lonely"])
    assert len(full) == 3  # noqa: PLR2004 — the three hub→leaf edges
    one = backend.edges_among("u1", ["hub", "leaf1"])
    assert [(e.src_node_id, e.dst_node_id) for e in one] == [("hub", "leaf1")]
    assert backend.edges_among("u1", ["lonely"]) == []  # an isolated node induces no edge
    assert backend.edges_among("u1", ["leaf1", "leaf2"]) == []  # no leaf↔leaf edge


def test_edges_among_is_owner_scoped(backend: PostgresGraphBackend) -> None:
    _seed_star(backend, "u1")
    assert backend.edges_among("u2", ["hub", "leaf1"]) == []  # u2 sees none of u1's edges


# ----- entity_edges_among (Spec K12, D-K12-A) -------------------------------


def _seed_two_nodes_sharing_entity(backend: PostgresGraphBackend, owner: str) -> None:
    """``n_a``/``n_b`` concern the same canonical entity; ``n_c`` concerns none.

    Mirrors the join-table-only substrate ``entity_neighbors``/``neighbors`` already
    traverse (D-K0-9) — no ``graph_edges`` row for the entity relation.
    """
    vec = [0.0] * DIM
    backend.insert_node(owner, _node("n_a", created_at=NOW), vec)
    backend.insert_node(owner, _node("n_b", created_at=NOW + timedelta(minutes=1)), vec)
    backend.insert_node(owner, _node("n_c", created_at=NOW + timedelta(minutes=2)), vec)
    entity = CanonicalEntity(
        id=f"{owner}::entity::00000001", canonical_name="Dr. Hansen", created_at=NOW
    )
    backend.insert_entity(owner, entity, vec)
    backend.associate_entities(owner, "n_a", [entity.id])
    backend.associate_entities(owner, "n_b", [entity.id])


def test_entity_edges_among_links_shared_entity_nodes(backend: PostgresGraphBackend) -> None:
    # two nodes associated with the same canonical entity, one without
    _seed_two_nodes_sharing_entity(backend, "u1")  # helper: insert nodes + entity + join rows
    edges = backend.entity_edges_among("u1", ["n_a", "n_b", "n_c"])
    kinds = {(e.src_node_id, e.dst_node_id, e.link_type) for e in edges}
    assert any(lt == LinkType.ENTITY for (_, _, lt) in kinds)
    assert all(lt == LinkType.ENTITY for (_, _, lt) in kinds)  # only entity edges here
    assert len(edges) == 1  # exactly one edge for the one shared-entity pair
    assert {edges[0].src_node_id, edges[0].dst_node_id} == {"n_a", "n_b"}
    assert "n_c" not in {edges[0].src_node_id, edges[0].dst_node_id}


def test_entity_edges_among_needs_both_endpoints_in_the_set(backend: PostgresGraphBackend) -> None:
    _seed_two_nodes_sharing_entity(backend, "u1")
    assert backend.entity_edges_among("u1", ["n_a", "n_c"]) == []  # no shared entity
    assert backend.entity_edges_among("u1", ["n_a"]) == []  # single node induces no edge


def test_entity_edges_among_excludes_merged_away_nodes(backend: PostgresGraphBackend) -> None:
    """K7-D-4: a consolidated-away node contributes no entity edge, even if it still
    carries the association row (consolidation doesn't clean up ``graph_node_entities``)."""
    from sqlalchemy import text

    _seed_two_nodes_sharing_entity(backend, "u1")
    with backend._engine.begin() as conn:  # noqa: SLF001 — test reaches into the backend engine
        conn.execute(
            text("UPDATE graph_nodes SET merged_into = :c WHERE id = :m AND owner_id = 'u1'"),
            {"c": "n_a", "m": "n_b"},
        )
    assert backend.entity_edges_among("u1", ["n_a", "n_b", "n_c"]) == []


def test_entity_edges_among_is_owner_scoped(backend: PostgresGraphBackend) -> None:
    _seed_two_nodes_sharing_entity(backend, "u1")
    assert backend.entity_edges_among("u2", ["n_a", "n_b"]) == []  # u2 sees none of u1's edges
