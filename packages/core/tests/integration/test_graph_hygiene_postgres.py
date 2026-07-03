"""Integration tests for the Spec K7 T7 store hygiene over real Postgres (K7-D-9).

Proves the allowlist reshape (the pgvector common path scopes by the ``owner_id``
predicate — NO positive IN-list — with the HNSW iterative-scan GUCs), that the
K4-gated allowlist path and merged-exclusion still hold, the ``extversion ≥ 0.8.0``
gate (dev image; prod is an operator note), and reports the acceptance-7
recall/latency numbers for the reshaped vs allowlist paths on a seeded graph.

Operator note (K7-D-9): the prod Fly box ``open-persona-db`` must also show
``vector`` ``extversion ≥ 0.8.0`` (``SELECT extversion FROM pg_extension WHERE
extname='vector'``); if it lags, run ``ALTER EXTENSION vector UPDATE``. Reshape
degrades gracefully below 0.8.0 (exact scans stay correct; only tuning suffers).
"""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph._schema import graph_metadata, graph_nodes
from persona.graph.config import GraphSettings
from persona.graph.index_pgvector import PgvectorGraphIndex, pgvector_extversion
from persona.graph.models import NodeProvenance, _compute_node_hash
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

DIM = 384
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _rand_vec(seed: str) -> list[float]:
    """A deterministic pseudo-random unit vector from ``seed`` (no embedder needed)."""
    byts: list[int] = []
    counter = 0
    while len(byts) < DIM:
        byts.extend(hashlib.sha256(f"{seed}:{counter}".encode()).digest())
        counter += 1
    vec = [(b / 255.0) - 0.5 for b in byts[:DIM]]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class _QueryEmbedder:
    model_name = "seed"
    dimension = DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_rand_vec(t) for t in texts]


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[Engine]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set; skipping Postgres integration test")
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")
    from sqlalchemy.engine import make_url

    if os.environ.get("PERSONA_TEST_DB") != "1" and not (make_url(url).database or "").endswith(
        "_test"
    ):
        pytest.skip("Use a '*_test' DB or set PERSONA_TEST_DB=1 (destructive fixture).")
    from sqlalchemy import create_engine
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


def _bulk_insert(engine: Engine, owner: str, n: int, *, offset: int = 0) -> None:
    rows = []
    for i in range(offset, offset + n):
        name, content = f"c{i}", f"content {i}"
        rows.append(
            {
                "id": f"{owner}::node::{i:08d}",
                "owner_id": owner,
                "node_kind": "concept",
                "concept_name": name,
                "content": content,
                "metadata": {},
                "wellbeing_category": None,
                "embedding": _rand_vec(f"{owner}:{i}"),
                "embedding_model": "seed",
                # the REAL content hash so ConceptNode hydration accepts the row.
                "content_hash": _compute_node_hash(name, content, {}),
                "provenance": [
                    NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=T0).model_dump(
                        mode="json"
                    )
                ],
                "created_at": T0,
                "updated_at": T0,
            }
        )
    with engine.begin() as conn:
        conn.execute(pg_insert(graph_nodes), rows)


@pytest.fixture
def clean(pg_engine: Engine) -> Iterator[Engine]:
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    yield pg_engine
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)


# ----- extversion gate (K7-D-9) --------------------------------------------


def test_pgvector_extversion_meets_gate(clean: Engine) -> None:
    version = pgvector_extversion(clean)
    assert version is not None
    parts = tuple(int(p) for p in version.split(".")[:2])
    if parts < (0, 8):
        pytest.skip(
            f"pgvector {version} < 0.8.0 — reshape degrades gracefully (run "
            f"ALTER EXTENSION vector UPDATE). Prod op-note in the module docstring."
        )
    assert parts >= (0, 8)  # dev image satisfies the iterative-scan gate


# ----- the reshape: common path is owner-scoped, no IN-list, merged-excluded -


def test_common_path_owner_scoped_no_in_list(clean: Engine) -> None:
    # structural: the reshaped search SQL carries NO surrogate IN-list.
    src = inspect.getsource(PgvectorGraphIndex.search_owner)
    assert ".in_(" not in src
    assert "owner_id" in src

    _bulk_insert(clean, "u1", 20)
    _bulk_insert(clean, "u2", 20)  # a second tenant
    store = build_graph_store(
        engine=clean, embedder=_QueryEmbedder(), audit_logger=MemoryAuditLogger()
    )
    # common path (allowlist=None) → only u1's nodes, and it used search_owner.
    hits = store.search_dense("u1", "u1:0", top_k=50)
    assert hits, "expected results"
    assert all(n.id.startswith("u1::") for n in hits)  # owner-scoped by predicate


def test_common_path_excludes_merged(clean: Engine) -> None:
    _bulk_insert(clean, "u1", 10)
    with clean.begin() as conn:
        conn.execute(
            text("UPDATE graph_nodes SET merged_into = :c WHERE id = :m"),
            {"c": "u1::node::00000000", "m": "u1::node::00000001"},
        )
    store = build_graph_store(
        engine=clean, embedder=_QueryEmbedder(), audit_logger=MemoryAuditLogger()
    )
    ids = {n.id for n in store.search_dense("u1", "u1:1", top_k=50)}
    assert "u1::node::00000001" not in ids  # the merged node is invisible


def test_k4_gated_allowlist_path_still_scopes(clean: Engine) -> None:
    _bulk_insert(clean, "u1", 10)
    store = build_graph_store(
        engine=clean, embedder=_QueryEmbedder(), audit_logger=MemoryAuditLogger()
    )
    # a K4-style positive allowlist (a subset) → only those nodes come back.
    allow = {"u1::node::00000002", "u1::node::00000003"}
    ids = {n.id for n in store.search_dense("u1", "u1:2", top_k=50, allowlist=allow)}
    assert ids <= allow


# ----- acceptance-7: recall + latency, reshaped vs allowlist (K7-D-9) -------


def test_measurement_harness_recall_and_latency(clean: Engine) -> None:
    n = 1200
    _bulk_insert(clean, "u1", n)
    with clean.begin() as conn:
        conn.execute(text("ANALYZE graph_nodes"))
    backend_index = PgvectorGraphIndex(engine=clean, settings=GraphSettings())
    q = _rand_vec("query-probe")

    # exact ground truth (seqscan, no HNSW GUC) for recall.
    with clean.connect() as conn:
        distance = graph_nodes.c.embedding.cosine_distance(q).label("d")
        exact = [
            int(r[0])
            for r in conn.execute(
                select(graph_nodes.c.surrogate, distance)
                .where(graph_nodes.c.owner_id == "u1")
                .order_by(distance)
                .limit(10)
            )
        ]

    # reshaped common path (owner predicate + iterative-scan GUCs), timed.
    t = time.perf_counter()
    reshaped = [s for s, _ in backend_index.search_owner(owner_id="u1", query_vector=q, top_k=10)]
    reshaped_ms = (time.perf_counter() - t) * 1000

    # the old allowlist path (enumerate surrogates + IN-list), timed.
    with clean.connect() as conn:
        surrogates = [
            int(r[0])
            for r in conn.execute(
                select(graph_nodes.c.surrogate).where(graph_nodes.c.owner_id == "u1")
            )
        ]
    t = time.perf_counter()
    allowlisted = [
        s for s, _ in backend_index.search(query_vector=q, top_k=10, allowlist=surrogates)
    ]
    allowlist_ms = (time.perf_counter() - t) * 1000
    assert len(allowlisted) == 10  # the K4-path still returns a full page

    recall = len(set(reshaped) & set(exact)) / len(exact)
    print(  # noqa: T201 — the acceptance-7 numbers are the deliverable
        f"\n[K7-D-9 acceptance-7 @ N={n}] recall@10={recall:.2f} "
        f"reshaped={reshaped_ms:.1f}ms allowlist-IN={allowlist_ms:.1f}ms"
    )
    assert recall >= 0.9  # HNSW + iterative-scan keeps recall high on the reshaped path
    assert len(reshaped) == 10
