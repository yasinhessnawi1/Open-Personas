"""Integration tests for the assembled GraphStore (Spec K0, T8) — end to end.

Drives ``build_graph_store`` against real Postgres on BOTH index backends (pgvector
default + turbovec opt-in, skipped if absent): merge→retrievable, delete→gone
(same-path sync), cross-owner isolation (criterion 6), rebuild equivalence
(criterion 9), FTS, entity-thread neighbors, and one-audit-per-merge.
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.graph._schema import graph_metadata
from persona.graph.config import GraphSettings
from persona.graph.entities import PostgresEntityRegistry
from persona.graph.models import LinkType, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.graph.protocol import KnowledgeCandidate
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

    _Stack = tuple[object, MemoryAuditLogger, Engine]

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
DIM = 384


def _vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


_MAP = {
    "likes coffee": _vec(0),
    "coffee query": _vec(0),
    "enjoys hiking": _vec(2),
    "takes metformin daily": _vec(4),
    "doctor visit one": _vec(6),
    "doctor visit two": _vec(7),
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
            keys = [k for k in _MAP if k in t]
            out.append(_MAP[max(keys, key=len)] if keys else _vec(100))
        return out


def _cand(content: str, **kw: object) -> KnowledgeCandidate:
    base: dict[str, object] = {
        "concept_name": content[:20],
        "content": content,
        "node_kind": NodeKind.FACT,
        "provenance": NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW),
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
        pass
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"Postgres unreachable: {exc}")
    yield engine
    engine.dispose()


def _settings(backend: str) -> GraphSettings:
    if backend == "turbovec":
        pytest.importorskip("turbovec")
        return GraphSettings(index_backend="turbovec", index_bit_width=4)
    return GraphSettings()


@pytest.fixture(params=["pgvector", "turbovec"])
def stack(request: pytest.FixtureRequest, _engine: Engine) -> Iterator[_Stack]:
    settings = _settings(request.param)
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    audit = MemoryAuditLogger()
    store = build_graph_store(
        engine=_engine,
        embedder=_Embedder(),
        audit_logger=audit,
        settings=settings,
        persona_guard=False,
    )
    yield store, audit, _engine
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)


def test_merge_then_search_dense_retrievable(stack: _Stack) -> None:
    store, _audit, _e = stack
    out = store.merge("u1", _cand("likes coffee"))  # type: ignore[attr-defined]
    hits = store.search_dense("u1", "coffee query", top_k=5)  # type: ignore[attr-defined]
    assert out.node_id in {n.id for n in hits}


def test_delete_then_not_retrievable_same_path_sync(
    stack: _Stack,
) -> None:
    store, _audit, _e = stack
    out = store.merge("u1", _cand("likes coffee"))  # type: ignore[attr-defined]
    assert store.delete_node("u1", out.node_id) is True  # type: ignore[attr-defined]
    hits = store.search_dense("u1", "coffee query", top_k=5)  # type: ignore[attr-defined]
    assert out.node_id not in {n.id for n in hits}


def test_cross_owner_isolation_via_allowlist(
    stack: _Stack,
) -> None:
    store, _audit, _e = stack
    a = store.merge("u1", _cand("likes coffee"))  # type: ignore[attr-defined]
    b = store.merge("u2", _cand("likes coffee"))  # same vector, different owner
    hits = store.search_dense("u1", "coffee query", top_k=10)  # type: ignore[attr-defined]
    ids = {n.id for n in hits}
    assert a.node_id in ids
    assert b.node_id not in ids  # criterion 6: owner allowlist excludes u2


def test_rebuild_index_keeps_retrieval(stack: _Stack) -> None:
    store, _audit, _e = stack
    out = store.merge("u1", _cand("likes coffee"))  # type: ignore[attr-defined]
    store.rebuild_index("u1")  # type: ignore[attr-defined]  # drop+re-add from Postgres
    hits = store.search_dense("u1", "coffee query", top_k=5)  # type: ignore[attr-defined]
    assert out.node_id in {n.id for n in hits}


def test_search_fts(stack: _Stack) -> None:
    store, _audit, _e = stack
    out = store.merge("u1", _cand("takes metformin daily"))  # type: ignore[attr-defined]
    store.merge("u1", _cand("enjoys hiking"))  # type: ignore[attr-defined]
    hits = store.search_fts("u1", "metformin", top_k=5)  # type: ignore[attr-defined]
    assert [n.id for n in hits] == [out.node_id]


def test_merge_emits_exactly_one_audit(stack: _Stack) -> None:
    store, audit, _e = stack
    store.merge("u1", _cand("likes coffee"))  # type: ignore[attr-defined]
    assert len(audit.events) == 1
    assert audit.events[0].action is AuditAction.WRITE
    assert audit.events[0].store == "knowledge_graph"


def test_neighbors_entity_thread(stack: _Stack) -> None:
    store, _audit, engine = stack
    registry = PostgresEntityRegistry(
        backend=PostgresGraphBackend(engine=engine, persona_guard=False), embedder=_Embedder()
    )
    ent = registry.create_entity("u1", canonical_name="Dr. Hansen")
    a = store.merge("u1", _cand("doctor visit one", entity_ids=(ent.id,)))  # type: ignore[attr-defined]
    b = store.merge("u1", _cand("doctor visit two", entity_ids=(ent.id,)))  # type: ignore[attr-defined]
    out = store.neighbors("u1", a.node_id, link_types={LinkType.ENTITY}, limit=10)  # type: ignore[attr-defined]
    assert {n.id for _, n in out} == {b.node_id}
    assert all(edge.link_type is LinkType.ENTITY for edge, _ in out)
