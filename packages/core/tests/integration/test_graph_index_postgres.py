"""Integration tests for the pgvector dense-index adapter (Spec K0, T7).

The default + only-wired-prod index: exact cosine over ``graph_nodes`` restricted
to the surrogate allowlist (identical allowlist semantics to turbovec). Self-
contained schema; deterministic unit vectors.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.graph._schema import EMBEDDING_DIM, graph_metadata
from persona.graph.index_pgvector import PgvectorGraphIndex
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.graph.protocol import GraphIndex
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _vec(i: int) -> list[float]:
    v = [0.0] * EMBEDDING_DIM
    v[i % EMBEDDING_DIM] = 1.0
    return v


def _node(node_id: str) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name="c",
        content="c",
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),),
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
def index_and_backend(_engine: Engine) -> Iterator[tuple[PgvectorGraphIndex, PostgresGraphBackend]]:
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    yield (
        PgvectorGraphIndex(engine=_engine),
        PostgresGraphBackend(engine=_engine, persona_guard=False),
    )
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)


def test_pgvector_index_satisfies_protocol(
    index_and_backend: tuple[PgvectorGraphIndex, PostgresGraphBackend],
) -> None:
    idx, _ = index_and_backend
    assert isinstance(idx, GraphIndex)


def test_search_ranks_by_cosine_over_graph_nodes(
    index_and_backend: tuple[PgvectorGraphIndex, PostgresGraphBackend],
) -> None:
    idx, backend = index_and_backend
    s1 = backend.insert_node("u1", _node("u1::node::00000001"), _vec(0))
    s2 = backend.insert_node("u1", _node("u1::node::00000002"), _vec(1))
    out = idx.search(query_vector=_vec(0), top_k=2, allowlist=[s1, s2])
    assert out[0][0] == s1
    assert out[0][1] > out[1][1]  # similarity (1 - distance)


def test_search_allowlist_restricts(
    index_and_backend: tuple[PgvectorGraphIndex, PostgresGraphBackend],
) -> None:
    idx, backend = index_and_backend
    s1 = backend.insert_node("u1", _node("u1::node::00000001"), _vec(0))
    backend.insert_node("u1", _node("u1::node::00000002"), _vec(1))
    out = idx.search(query_vector=_vec(1), top_k=5, allowlist=[s1])
    assert [s for s, _ in out] == [s1]


def test_search_empty_allowlist_returns_nothing(
    index_and_backend: tuple[PgvectorGraphIndex, PostgresGraphBackend],
) -> None:
    idx, backend = index_and_backend
    backend.insert_node("u1", _node("u1::node::00000001"), _vec(0))
    assert idx.search(query_vector=_vec(0), top_k=5, allowlist=[]) == []


def test_contains_reflects_table(
    index_and_backend: tuple[PgvectorGraphIndex, PostgresGraphBackend],
) -> None:
    idx, backend = index_and_backend
    s1 = backend.insert_node("u1", _node("u1::node::00000001"), _vec(0))
    assert idx.contains(s1)
    backend.delete_node("u1", "u1::node::00000001")
    assert not idx.contains(s1)


def test_mutators_are_noops_table_is_the_index(
    index_and_backend: tuple[PgvectorGraphIndex, PostgresGraphBackend],
) -> None:
    idx, backend = index_and_backend
    s1 = backend.insert_node("u1", _node("u1::node::00000001"), _vec(0))
    # add/replace/rebuild/persist do nothing; the row already carries the embedding.
    idx.add(surrogate=s1, vector=_vec(0))
    idx.replace(surrogate=s1, vector=_vec(0))
    idx.rebuild([(s1, _vec(0))])
    idx.persist()
    assert idx.contains(s1)
    assert idx.search(query_vector=_vec(0), top_k=1, allowlist=[s1])[0][0] == s1
