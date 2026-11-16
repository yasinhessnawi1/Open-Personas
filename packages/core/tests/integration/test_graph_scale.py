"""Scale check — the store + index hold and query a large graph (Spec K0, criterion 12).

Bulk-loads a realistically large per-user graph, (re)builds the dense index from
Postgres, and asserts queries stay fast AND correct on BOTH backends (turbovec
in-RAM quantized + pgvector HNSW). ``@external`` (heavy load); skips without
turbovec for that leg. Crafted random unit vectors — no model load.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph._schema import graph_metadata, graph_nodes
from persona.graph.config import GraphSettings
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance, make_node_id
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.external

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
DIM = 384
_N = 3000  # ≥ the TQ+ calibration floor; "realistically large" for v0.1
_QUERIES = 50
# Per-backend budgets: turbovec is the in-RAM quantized index criterion 12 targets
# (tight). pgvector is the exact default; at scale its dense leg carries a redundant
# owner-surrogate IN clause (RLS already scopes it) that defeats HNSW — a documented
# v0.2 optimization seam (rely on RLS for pgvector / drop the IN), hence the looser
# bound here. Both are well within a per-turn budget; neither degrades.
_BUDGET_MS = {"turbovec": 50.0, "pgvector": 150.0}


class _StaticEmbedder:
    """Returns a pre-set query vector (the scale test drives vectors directly)."""

    model_name = "static"

    def __init__(self) -> None:
        self.next_vector: list[float] = [0.0] * DIM

    @property
    def dimension(self) -> int:
        return DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self.next_vector for _ in texts]


def _unit_rows(n: int) -> tuple[list[dict[str, object]], list[list[float]]]:
    import numpy as np

    rng = np.random.default_rng(7)
    raw = rng.standard_normal((n, DIM)).astype(np.float32)
    raw /= np.linalg.norm(raw, axis=1, keepdims=True)
    vecs = [[float(x) for x in row] for row in raw]
    prov = NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW)
    prov_json = [prov.model_dump(mode="json")]
    rows: list[dict[str, object]] = []
    for i in range(n):
        # Build via ConceptNode so content_hash matches (the hydration tamper-check).
        node = ConceptNode(
            id=make_node_id("u1", i),
            node_kind=NodeKind.FACT,
            concept_name="c",
            content=f"node {i}",
            provenance=(prov,),
            created_at=NOW,
        )
        rows.append(
            {
                "id": node.id,
                "owner_id": "u1",
                "node_kind": "fact",
                "concept_name": node.concept_name,
                "content": node.content,
                "metadata": {},
                "wellbeing_category": None,
                "embedding": vecs[i],
                "embedding_model": "static",
                "content_hash": node.content_hash,
                "provenance": prov_json,
                "created_at": NOW,
            }
        )
    return rows, vecs


@pytest.fixture(scope="session")
def graph_engine() -> Iterator[Engine]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set")
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")
    from sqlalchemy.engine import make_url

    db_name = make_url(url).database or ""
    if os.environ.get("PERSONA_TEST_DB") != "1" and not db_name.endswith("_test"):
        pytest.skip("Use a '*_test' DB or set PERSONA_TEST_DB=1.")
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


@pytest.mark.parametrize("backend", ["pgvector", "turbovec"])
def test_store_holds_and_queries_a_large_graph(graph_engine: Engine, backend: str) -> None:
    if backend == "turbovec":
        pytest.importorskip("turbovec")
    settings = GraphSettings(index_backend="turbovec") if backend == "turbovec" else GraphSettings()
    with graph_engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    try:
        rows, vecs = _unit_rows(_N)
        with graph_engine.begin() as conn:
            conn.execute(graph_nodes.insert(), rows)  # one bulk executemany

        embedder = _StaticEmbedder()
        store = build_graph_store(
            engine=graph_engine,
            embedder=embedder,
            audit_logger=MemoryAuditLogger(),
            settings=settings,
            persona_guard=False,
        )
        store.rebuild_index("u1")  # build the index from Postgres at scale (criterion 9 + 12)

        # Correctness at scale: a query equal to a known node's vector finds it.
        target_id = make_node_id("u1", 1234)
        embedder.next_vector = vecs[1234]
        hits = store.search_dense("u1", "q", top_k=10)
        assert target_id in {n.id for n in hits[:5]}

        # No degradation: per-query latency stays within budget over a batch.
        timings: list[float] = []
        for i in range(_QUERIES):
            embedder.next_vector = vecs[i * 37 % _N]
            t = time.perf_counter()
            store.search_dense("u1", "q", top_k=10)
            timings.append((time.perf_counter() - t) * 1000.0)
        timings.sort()
        p95 = timings[int(len(timings) * 0.95)]
        assert p95 < _BUDGET_MS[backend], f"{backend} p95={p95:.1f}ms over {_N} nodes"
    finally:
        with graph_engine.begin() as conn:
            graph_metadata.drop_all(conn)
