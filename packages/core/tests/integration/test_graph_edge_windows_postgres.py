"""Integration tests for the Spec K7 T3 edge valid-time windows over real Postgres.

Proves against real SQL (the partial-unique-open-edge index, the window CHECK, the
Zep point-in-time predicate): ``invalidate_edge`` closes-not-deletes; re-asserting a
closed fact opens a NEW row so two rows share the deterministic ``id`` (one closed,
one open); re-asserting an already-open edge does not duplicate; ``neighbors``
defaults open-only and ``as_of`` returns exactly the window-covering edges; and the
new reads stay owner-scoped (a non-vacuous cross-tenant probe).
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.graph._schema import graph_edges, graph_metadata
from persona.graph.models import (
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_edge_id,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource
from sqlalchemy import func, select

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

DIM = 384
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(days=30)
T2 = T0 + timedelta(days=60)


def _vec(i: int) -> list[float]:
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    v[382] = math.sqrt(max(0.0, 1.0 - 1.0))
    return v


def _prov() -> NodeProvenance:
    return NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=T0)


def _node(node_id: str) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name="c",
        content="c",
        provenance=(_prov(),),
        created_at=T0,
    )


def _edge(src: str, dst: str, kind: LinkType, *, valid_at: datetime = T0) -> TypedLink:
    return TypedLink(
        id=make_edge_id(src, dst, kind),
        src_node_id=src,
        dst_node_id=dst,
        link_type=kind,
        created_at=T0,
        valid_at=valid_at,
    )


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
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
def backend(pg_engine: Engine) -> Iterator[PostgresGraphBackend]:
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    b = PostgresGraphBackend(engine=pg_engine)
    # two nodes so an edge's composite FK holds.
    b.insert_node("u1", _node("u1::node::00000000"), _vec(0))
    b.insert_node("u1", _node("u1::node::00000001"), _vec(1))
    yield b
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)


def _rows_for_id(engine: Engine, edge_id: str) -> list[dict]:
    stmt = select(graph_edges).where(graph_edges.c.id == edge_id).order_by(graph_edges.c.edge_key)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


A = "u1::node::00000000"
B = "u1::node::00000001"


# ----- invalidate closes-not-deletes; re-assert opens a NEW row (K7-D-1) ----


def test_invalidate_then_reassert_yields_two_rows_sharing_id(
    backend: PostgresGraphBackend, pg_engine: Engine
) -> None:
    edge = _edge(A, B, LinkType.TEMPORAL, valid_at=T0)
    backend.upsert_edge("u1", edge)
    assert len(_rows_for_id(pg_engine, edge.id)) == 1  # one open row

    assert backend.invalidate_edge("u1", edge.id, invalidated_by="ref-1", invalid_at=T1) is True
    rows = _rows_for_id(pg_engine, edge.id)
    assert len(rows) == 1  # closed-not-deleted (§0): still one row, now with a window end
    assert rows[0]["invalid_at"] is not None
    assert rows[0]["invalidated_by"] == "ref-1"

    # re-assert the SAME fact after close → a NEW open row (partial-unique lets it in).
    backend.upsert_edge("u1", _edge(A, B, LinkType.TEMPORAL, valid_at=T2))
    rows = _rows_for_id(pg_engine, edge.id)
    assert len(rows) == 2  # two rows share the deterministic id…
    assert {r["id"] for r in rows} == {edge.id}
    assert len({r["edge_key"] for r in rows}) == 2  # …distinct surrogate keys
    open_rows = [r for r in rows if r["invalid_at"] is None]
    closed_rows = [r for r in rows if r["invalid_at"] is not None]
    assert len(open_rows) == 1  # exactly one open (the partial-unique invariant)
    assert len(closed_rows) == 1


def test_reasserting_open_edge_does_not_duplicate(
    backend: PostgresGraphBackend, pg_engine: Engine
) -> None:
    edge = _edge(A, B, LinkType.CAUSAL, valid_at=T0)
    backend.upsert_edge("u1", edge)
    backend.upsert_edge("u1", edge)  # replay onto the open row (partial-unique conflict)
    rows = _rows_for_id(pg_engine, edge.id)
    assert len(rows) == 1  # refreshed, not duplicated


def test_invalidate_is_idempotent(backend: PostgresGraphBackend) -> None:
    edge = _edge(A, B, LinkType.TEMPORAL, valid_at=T0)
    backend.upsert_edge("u1", edge)
    assert backend.invalidate_edge("u1", edge.id, invalidated_by="x") is True
    # closing an already-closed edge is a no-op.
    assert backend.invalidate_edge("u1", edge.id, invalidated_by="x") is False
    # a missing/other edge is a no-op too.
    assert backend.invalidate_edge("u1", "nope::temporal::none", invalidated_by="x") is False


# ----- neighbors: open-only default + point-in-time as_of (K7-D-1) ----------


def test_neighbors_open_only_and_point_in_time(backend: PostgresGraphBackend) -> None:
    edge = _edge(A, B, LinkType.TEMPORAL, valid_at=T0)
    backend.upsert_edge("u1", edge)
    # open → visible by default.
    assert [n.id for _, n in backend.neighbors("u1", A, limit=10)] == [B]
    # close the window at T1.
    backend.invalidate_edge("u1", edge.id, invalidated_by="ref", invalid_at=T1)
    # default (open-only) → gone.
    assert backend.neighbors("u1", A, limit=10) == []
    # as_of INSIDE the window [T0, T1) → visible.
    inside = backend.neighbors("u1", A, limit=10, as_of=T0 + timedelta(days=1))
    assert [n.id for _, n in inside] == [B]
    # as_of AFTER the close → invisible.
    assert backend.neighbors("u1", A, limit=10, as_of=T1 + timedelta(days=1)) == []
    # as_of BEFORE the window opens → invisible.
    assert backend.neighbors("u1", A, limit=10, as_of=T0 - timedelta(days=1)) == []


# ----- owner-scoping of the new reads (non-vacuous cross-tenant) ------------


def test_neighbors_is_owner_scoped(backend: PostgresGraphBackend, pg_engine: Engine) -> None:
    # seed a second tenant with its own node + edge; u1's traversal must never see it.
    backend.insert_node("u2", _node("u2::node::00000000"), _vec(0))
    backend.insert_node("u2", _node("u2::node::00000001"), _vec(1))
    backend.upsert_edge("u1", _edge(A, B, LinkType.TEMPORAL, valid_at=T0))
    backend.upsert_edge(
        "u2",
        _edge("u2::node::00000000", "u2::node::00000001", LinkType.TEMPORAL, valid_at=T0),
    )
    # non-vacuous: u2 genuinely has an open edge…
    assert [n.id for _, n in backend.neighbors("u2", "u2::node::00000000", limit=10)] == [
        "u2::node::00000001"
    ]
    # …but u1's traversal only ever returns u1's neighbour, never u2's.
    u1_neighbours = {n.id for _, n in backend.neighbors("u1", A, limit=10)}
    assert u1_neighbours == {B}
    assert all(nid.startswith("u1::") for nid in u1_neighbours)
    # and a global row count confirms both tenants' edges coexist (probe is real).
    with pg_engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(graph_edges)).scalar_one()
    assert total == 2
