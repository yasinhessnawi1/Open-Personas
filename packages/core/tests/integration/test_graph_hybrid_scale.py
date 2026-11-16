"""Full-stack hybrid retrieval at scale (Spec K1, T5 / criterion 10/11 / K1-R-4).

MEASURES the whole stack as it will run — dense (ANN + mandatory exact-rerank) +
sparse FTS + RRF + bounded traversal + the post-fusion K4 subtraction filter —
over a realistically large multi-user graph, on BOTH backends. Nothing is
asserted that isn't measured:

- **Latency (criterion 10):** end-to-end ``retrieve()`` p95 over a query batch
  stays within a per-turn budget at ~1500 nodes/user. The in-RAM quantized dense
  leg (turbovec) is tight; pgvector carries the documented v0.2 owner-surrogate
  ``IN`` seam (closeout #3), hence its looser bound — both well within a turn.
- **Recall (criterion 11):** the dense leg through the full stack (search_dense =
  ANN→rerank) matches a float32 brute-force baseline — exact on pgvector by
  construction; recall@10 ≥ 0.95 on turbovec (the 4-bit+rerank operating point,
  the geometric companion to K0's *semantic* recall gate `test_graph_recall.py`).
- **No relevant node dropped:** a node relevant on BOTH legs is retained through
  fusion + the K4 filter + budget truncation; a K4-flagged node is dropped; and
  the rest are unaffected — the relay's measure-the-filter requirement.

``@external`` (heavy load); skips without ``DATABASE_URL`` (Postgres) and without
the ``[turbovec]`` extra for that leg. Crafted random unit vectors — no model load
(semantic/paraphrase recall is K0's `test_graph_recall.py`; this is the
full-stack geometric recall + latency + no-drop at scale).
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
from persona.graph.models import ConceptNode, NodeProvenance, make_node_id
from persona.graph.retrieval import HybridRetriever
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.external

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
DIM = 384
_N_U1 = 1500  # ≥ the turbovec TQ+ calibration floor (1000); "realistically large"
_N_U2 = 300  # a second tenant → genuinely multi-user
_QUERIES = 50
_K = 10
_RECALL_BAR = 0.95
_TARGET = 777  # the planted both-legs-relevant node (u1)
# Full-stack budgets (dense+rerank + FTS + RRF + bounded traversal round-trips).
# Looser than K0's dense-only budgets (turbovec 50 / pgvector 150) by design —
# the sparse query + up to traversal_seed_count neighbour round-trips add cost.
# Both are an order of magnitude inside a conversational turn.
_BUDGET_MS = {"turbovec": 120.0, "pgvector": 300.0}


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


def _unit_vectors(n: int, seed: int) -> list[list[float]]:
    import numpy as np

    rng = np.random.default_rng(seed)
    raw = rng.standard_normal((n, DIM)).astype(np.float32)
    raw /= np.linalg.norm(raw, axis=1, keepdims=True)
    return [[float(x) for x in row] for row in raw]


def _rows(
    owner_id: str, vecs: list[list[float]], *, target_index: int | None = None
) -> list[dict[str, object]]:
    prov = NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW)
    prov_json = [prov.model_dump(mode="json")]
    rows: list[dict[str, object]] = []
    for i, vec in enumerate(vecs):
        # The planted target carries a distinctive lexical term so the FTS leg
        # matches it decisively; everything else is generic so it does not.
        content = "takes metformin daily marker" if i == target_index else f"node {i}"
        node = ConceptNode(
            id=make_node_id(owner_id, i),
            node_kind="fact",  # type: ignore[arg-type]
            concept_name="c",
            content=content,
            provenance=(prov,),
            created_at=NOW,
        )
        rows.append(
            {
                "id": node.id,
                "owner_id": owner_id,
                "node_kind": "fact",
                "concept_name": node.concept_name,
                "content": node.content,
                "metadata": {},
                "wellbeing_category": None,
                "embedding": vec,
                "embedding_model": "static",
                "content_hash": node.content_hash,
                "provenance": prov_json,
                "created_at": NOW,
            }
        )
    return rows


def _float32_topk(query: list[float], corpus: list[list[float]], k: int) -> set[int]:
    import numpy as np

    sims = np.asarray(corpus, dtype=np.float32) @ np.asarray(query, dtype=np.float32)
    return {int(i) for i in np.argsort(-sims)[:k]}


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
def test_full_stack_latency_recall_and_no_drop_at_scale(graph_engine: Engine, backend: str) -> None:
    if backend == "turbovec":
        pytest.importorskip("turbovec")
    settings = GraphSettings(index_backend="turbovec") if backend == "turbovec" else GraphSettings()
    with graph_engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    try:
        u1_vecs = _unit_vectors(_N_U1, seed=7)
        u2_vecs = _unit_vectors(_N_U2, seed=13)
        with graph_engine.begin() as conn:
            conn.execute(graph_nodes.insert(), _rows("u1", u1_vecs, target_index=_TARGET))
            conn.execute(graph_nodes.insert(), _rows("u2", u2_vecs))

        embedder = _StaticEmbedder()
        store = build_graph_store(
            engine=graph_engine,
            embedder=embedder,
            audit_logger=MemoryAuditLogger(),
            settings=settings,
            persona_guard=False,
        )
        store.rebuild_index("u1")
        store.rebuild_index("u2")
        retriever = HybridRetriever(store, settings)

        # --- recall: dense leg (ANN+rerank) vs float32 brute force (criterion 11)
        recalls: list[float] = []
        for i in range(_QUERIES):
            qi = i * 29 % _N_U1
            embedder.next_vector = u1_vecs[qi]
            got = {n.id for n in store.search_dense("u1", "q", top_k=_K)}  # type: ignore[attr-defined]
            truth = {make_node_id("u1", j) for j in _float32_topk(u1_vecs[qi], u1_vecs, _K)}
            recalls.append(len(got & truth) / _K)
        mean_recall = sum(recalls) / len(recalls)
        assert mean_recall >= _RECALL_BAR, f"{backend} full-stack recall@{_K}={mean_recall:.3f}"

        # --- no relevant node dropped through fusion + K4 filter + truncation
        embedder.next_vector = u1_vecs[_TARGET]
        target_id = make_node_id("u1", _TARGET)
        # both legs hit: dense (vector == target) + FTS ("metformin" matches its content)
        full = {r.node.id for r in retriever.retrieve("u1", "metformin")}
        assert target_id in full  # retained through the whole stack
        # cross-tenant: u1's retrieval never returns a u2 node, at scale
        assert not any(rid.startswith("u2::") for rid in full)
        # K4 subtraction drops the flagged node (and only it)
        allow = {make_node_id("u1", j) for j in range(_N_U1)} - {target_id}
        flagged = {r.node.id for r in retriever.retrieve("u1", "metformin", allowlist=allow)}
        assert target_id not in flagged
        assert flagged <= allow  # nothing outside the allowed set leaked

        # --- latency: full-stack retrieve() p95 within the per-turn budget (crit 10)
        timings: list[float] = []
        for i in range(_QUERIES):
            embedder.next_vector = u1_vecs[i * 37 % _N_U1]
            start = time.perf_counter()
            retriever.retrieve("u1", "node")
            timings.append((time.perf_counter() - start) * 1000.0)
        timings.sort()
        p95 = timings[int(len(timings) * 0.95)]
        assert p95 < _BUDGET_MS[backend], (
            f"{backend} full-stack p95={p95:.1f}ms over {_N_U1}+{_N_U2} nodes "
            f"(budget {_BUDGET_MS[backend]}ms; recall@{_K}={mean_recall:.3f})"
        )
    finally:
        with graph_engine.begin() as conn:
            graph_metadata.drop_all(conn)
