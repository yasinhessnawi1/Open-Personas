"""HNSW ANN latency over 10k chunks (spec 07, T07, acceptance #6).

Seeds 10,000 memory_chunks and asserts a cosine `<=>` ANN query (at the
EpisodicStore fetch-k of top_k*3) returns under 50ms at p95. The Phase-3 spike
measured ~32ms p95; this guards against a regression (e.g. a missing/!cosine
index, or a seq-scan introduced by an RLS policy).
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

# R9-145: this is a latency BENCHMARK, not a correctness test. It seeds 10,000 rows and
# asserts a p95 under 50 ms, which a loaded laptop fails on timing alone; under the
# 120 s integration timeout it died mid-query and psycopg's "cannot exit pipeline mode
# while busy" turned that into a runner internal error (exit 3) at 49% of the leg. It
# runs in the opt-in soak suite with its own budget; the regression it guards (a
# missing cosine index, a seq scan from an RLS policy) is still caught there.
pytestmark = [pytest.mark.soak, pytest.mark.timeout(900)]

_N = 10_000
_TOP_K = 3
_FETCH_K = max(_TOP_K * 3, _TOP_K)  # EpisodicStore.query candidate count
_LATENCY_BUDGET_MS = 50.0


def test_hnsw_ann_query_under_50ms_at_10k(
    migrated_engine: Engine, embedder: HashEmbedder384
) -> None:
    # Seed owner + persona (FK), then 10k chunks with deterministic vectors.
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1','u1@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1','u1','name: p1')")
        )
        rng = random.Random(11)
        rows = []
        vecs = embedder.encode([f"chunk text {i} {rng.random()}" for i in range(_N)])
        for i, vec in enumerate(vecs):
            rows.append(
                {
                    "id": f"c{i}",
                    "embedding": "[" + ",".join(repr(x) for x in vec) + "]",
                }
            )
        conn.execute(
            text(
                "INSERT INTO memory_chunks "
                "(id, persona_id, kind, text, embedding, content_hash) "
                "VALUES (:id, 'p1', 'episodic', 'note', :embedding, 'h')"
            ),
            rows,
        )

    q = embedder.encode(["a query about something"])[0]
    q_literal = "[" + ",".join(repr(x) for x in q) + "]"
    latencies: list[float] = []
    with migrated_engine.connect() as conn:
        for _ in range(30):
            t0 = time.perf_counter()
            conn.execute(
                text(
                    "SELECT id, embedding <=> :q ::vector AS distance FROM memory_chunks "
                    "WHERE persona_id = 'p1' AND kind = 'episodic' "
                    "ORDER BY embedding <=> :q ::vector LIMIT :k"
                ),
                {"q": q_literal, "k": _FETCH_K},
            ).all()
            latencies.append((time.perf_counter() - t0) * 1000)
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    assert p95 < _LATENCY_BUDGET_MS, f"HNSW ANN p95={p95:.1f}ms exceeds {_LATENCY_BUDGET_MS}ms"
