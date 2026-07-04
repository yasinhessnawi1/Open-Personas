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
from persona.graph.models import ConceptNode, LinkType, NodeKind, NodeProvenance, TypedLink
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
