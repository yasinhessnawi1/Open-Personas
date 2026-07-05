"""Integration test — the A5 ``recent_nodes`` SQL over real Postgres (A5-D-X-reads).

Proves the read the noticing pool is born from: salience DESC then updated_at
DESC ordering (K7's evidence signal as pool ORDERING, never a retrieval gate),
and the three at-the-read exclusions — the SELF anchor, merged members, and
EVERY wellbeing-tagged node (the initiative subject-exclusion,
A5-D-X-k4-initiative-side) — plus owner scoping (non-vacuous: both owners hold
rows; each read returns only its own).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.graph._schema import graph_metadata, graph_nodes
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource
from sqlalchemy import update

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

DIM = 384
T0 = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def _vec() -> list[float]:
    v = [0.0] * DIM
    v[0] = 1.0
    return v


def _node(
    node_id: str, *, kind: NodeKind = NodeKind.FACT, wellbeing: str | None = None
) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=kind,
        concept_name=node_id,
        content=f"content of {node_id}",
        wellbeing_category=wellbeing,
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=T0),),
        created_at=T0,
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
    yield PostgresGraphBackend(engine=pg_engine)
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)


def _set(engine: Engine, node_id: str, **values: object) -> None:
    with engine.begin() as conn:
        conn.execute(update(graph_nodes).where(graph_nodes.c.id == node_id).values(**values))


def test_recent_nodes_orders_by_salience_then_recency_and_excludes_at_the_read(
    backend: PostgresGraphBackend, pg_engine: Engine
) -> None:
    owner, other = "user_a", "user_b"
    # Owner A: two admissible nodes + one of each excluded shape.
    for node_id, kind, wellbeing in (
        ("a::plain_low", NodeKind.FACT, None),
        ("a::plain_high", NodeKind.GOAL, None),
        ("a::self", NodeKind.SELF, None),
        ("a::tagged", NodeKind.CIRCUMSTANCE, "self_harm"),
        ("a::merged", NodeKind.FACT, None),
    ):
        backend.insert_node_if_absent(owner, _node(node_id, kind=kind, wellbeing=wellbeing), _vec())
    backend.insert_node_if_absent(other, _node("b::theirs"), _vec())

    _set(pg_engine, "a::merged", merged_into="a::plain_high")
    # Salience/recency shaping: high-salience beats newer-but-lower.
    _set(pg_engine, "a::plain_high", salience=2.0, updated_at=T0)
    _set(pg_engine, "a::plain_low", salience=1.0, updated_at=T0 + timedelta(days=1))

    pool = backend.recent_nodes(owner, limit=10)
    assert [n.id for n in pool] == ["a::plain_high", "a::plain_low"]

    # limit binds.
    assert [n.id for n in backend.recent_nodes(owner, limit=1)] == ["a::plain_high"]

    # Non-vacuous owner scoping: B has a row and sees ONLY it.
    assert [n.id for n in backend.recent_nodes(other, limit=10)] == ["b::theirs"]


def test_recent_nodes_equal_salience_orders_by_updated_at(
    backend: PostgresGraphBackend, pg_engine: Engine
) -> None:
    owner = "user_c"
    backend.insert_node_if_absent(owner, _node("c::older"), _vec())
    backend.insert_node_if_absent(owner, _node("c::newer"), _vec())
    _set(pg_engine, "c::older", salience=1.0, updated_at=T0)
    _set(pg_engine, "c::newer", salience=1.0, updated_at=T0 + timedelta(hours=6))

    assert [n.id for n in backend.recent_nodes(owner, limit=10)] == ["c::newer", "c::older"]
