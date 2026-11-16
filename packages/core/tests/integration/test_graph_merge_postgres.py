"""Integration tests for the merge engine over real Postgres+pgvector (Spec K0, T6a).

Drives ``MergeEngine`` against the real ``PostgresGraphBackend`` so the dense-query
SQL, ``update_node`` extend path, ``count_nodes``, ``delete_links_from``, and edge
upserts are all exercised. A mapping embedder injects controlled vectors so real
pgvector cosine ranks deterministically.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.graph._schema import graph_metadata
from persona.graph.merge import MergeEngine
from persona.graph.models import CanonicalEntity, LinkType, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.graph.protocol import KnowledgeCandidate, MergeAction, UpdateIntent
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

NOW_OK = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
DIM = 384


def _vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


_MAP = {
    "likes coffee": _vec(0),
    "loves espresso": _vec(0, 0.95, 1),  # cos 0.95 → extends coffee
    "enjoys hiking": _vec(2),  # cos 0 → new node
    "likes tea": _vec(0, 0.85, 3),  # cos 0.85 → distinct node, but semantic-linked
    "quit the gym": _vec(2),  # contradiction target shares hiking's vector slot
    "saw the doctor about sleep": _vec(5),  # distinct nodes both about one entity
    "the doctor prescribed melatonin": _vec(6),
}


class _Embedder:
    model_name = "mapping"

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            if t in _MAP:
                out.append(_MAP[t])
                continue
            keys = [k for k in _MAP if k in t]  # accumulated content → anchor substring
            if not keys:
                raise KeyError(t)
            out.append(_MAP[max(keys, key=len)])
        return out


def _cand(content: str, **kw: object) -> KnowledgeCandidate:
    base: dict[str, object] = {
        "concept_name": content[:20],
        "content": content,
        "node_kind": NodeKind.FACT,
        "provenance": NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW_OK),
    }
    base.update(kw)
    return KnowledgeCandidate(**base)  # type: ignore[arg-type]


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
        pass  # concurrent CREATE EXTENSION IF NOT EXISTS catalog race; the extension exists
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"Postgres unreachable: {exc}")
    yield engine
    engine.dispose()


@pytest.fixture
def merge(_engine: Engine) -> Iterator[tuple[MergeEngine, PostgresGraphBackend]]:
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    backend = PostgresGraphBackend(engine=_engine, persona_guard=False)
    yield MergeEngine(backend=backend, embedder=_Embedder()), backend
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)


def test_extend_vs_create_round_trip(merge: tuple[MergeEngine, PostgresGraphBackend]) -> None:
    engine, backend = merge
    a = engine.merge("u1", _cand("likes coffee"))
    assert a.action is MergeAction.CREATED
    b = engine.merge("u1", _cand("loves espresso"))  # related → extend
    assert b.action is MergeAction.EXTENDED
    assert b.node_id == a.node_id
    c = engine.merge("u1", _cand("enjoys hiking"))  # unrelated → create
    assert c.action is MergeAction.CREATED
    assert backend.count_nodes("u1") == 2
    coffee = backend.get_node("u1", a.node_id)
    assert coffee is not None
    assert "likes coffee" in coffee.content
    assert "loves espresso" in coffee.content
    assert len(coffee.provenance) == 2  # accumulation trail grew


def test_idempotent_re_merge(merge: tuple[MergeEngine, PostgresGraphBackend]) -> None:
    engine, backend = merge
    engine.merge("u1", _cand("likes coffee"))
    engine.merge("u1", _cand("likes coffee"))  # identical re-merge
    assert backend.count_nodes("u1") == 1
    node = backend.get_node("u1", "u1::node::00000000")
    assert node is not None
    assert len(node.provenance) == 1  # no trail growth


def test_semantic_link_persisted_between_navigable_nodes(
    merge: tuple[MergeEngine, PostgresGraphBackend],
) -> None:
    engine, backend = merge
    a = engine.merge("u1", _cand("likes coffee"))
    b = engine.merge("u1", _cand("likes tea"))  # cos 0.85 → distinct + linked
    assert backend.count_nodes("u1") == 2
    neighbours = backend.neighbors("u1", a.node_id, link_types={LinkType.SEMANTIC}, limit=10)
    assert b.node_id in {n.id for _, n in neighbours}


def test_contradiction_evolves_node_with_provenance(
    merge: tuple[MergeEngine, PostgresGraphBackend],
) -> None:
    engine, backend = merge
    created = engine.merge("u1", _cand("enjoys hiking"))
    engine.merge(
        "u1",
        _cand(
            "quit the gym",
            update_intent=UpdateIntent.CONTRADICT,
            target_node_id=created.node_id,
        ),
    )
    node = backend.get_node("u1", created.node_id)
    assert node is not None
    assert node.content == "quit the gym"
    assert len(node.provenance) == 2
    assert node.provenance[-1].superseded_content == "enjoys hiking"


def test_entity_association_and_thread_traversal(
    merge: tuple[MergeEngine, PostgresGraphBackend],
) -> None:
    # criterion 2 over real DB: two distinct nodes about ONE entity → entity thread.
    engine, backend = merge
    ent = CanonicalEntity(id="u1::entity::00000000", canonical_name="Dr. Hansen", created_at=NOW_OK)
    backend.insert_entity("u1", ent, _vec(7))
    a = engine.merge("u1", _cand("saw the doctor about sleep", entity_ids=(ent.id,)))
    b = engine.merge("u1", _cand("the doctor prescribed melatonin", entity_ids=(ent.id,)))
    assert backend.count_nodes("u1") == 2  # distinct concepts…
    # …both threaded to the one entity (criterion 2: "all nodes for Dr. Hansen").
    assert {n.id for n in backend.nodes_for_entity("u1", ent.id)} == {a.node_id, b.node_id}
    assert backend.entities_for_node("u1", a.node_id) == [ent.id]
    # on-the-fly ENTITY traversal: a's entity-neighbour is b (no materialised edge).
    assert {n.id for n in backend.entity_neighbors("u1", a.node_id)} == {b.node_id}
    assert not backend.neighbors("u1", a.node_id, link_types={LinkType.ENTITY}, limit=10)
