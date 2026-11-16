"""Integration tests for HybridRetriever over a real graph (Spec K1, T4).

Drives the assembled ``build_graph_store`` + :class:`HybridRetriever` against real
Postgres on BOTH index backends (pgvector default + turbovec opt-in, skipped if
absent). Proves the spec's load-bearing properties end-to-end:

- §3 paraphrase case — dense finds meaning with no lexical overlap (criterion 1/4).
- §3 exact-term case — sparse finds the term dense ranks poorly (criterion 3/4).
- §4 no-gating — a paraphrase-only match survives fusion (criterion 5).
- allowlist — cross-owner isolation + the K4 subtraction over BOTH legs (criterion 6).
- typed-link traversal surfaces a node the flat query missed (criterion 7).
- index-sync observable from the retrieval side (criterion 8).
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph._schema import graph_metadata
from persona.graph.config import GraphSettings
from persona.graph.entities import PostgresEntityRegistry
from persona.graph.models import LinkType, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.graph.protocol import KnowledgeCandidate
from persona.graph.retrieval import HybridRetriever
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

    _Stack = tuple[object, Engine]

pytestmark = pytest.mark.integration

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
DIM = 384


def _vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


# The §3 cases by construction:
#  - the "worked examples" preference aligns (dense) with a "how do i learn"
#    query that shares NO lexical tokens with it (paraphrase → dense-only).
#  - the "metformin" query embeds AWAY from the metformin node and TOWARD a
#    "managing health" distractor (so dense ranks the term poorly) while FTS
#    finds the exact term decisively.
#  - the entity-thread nodes are lexically + semantically disjoint from each
#    other and from the query, so the second only surfaces via ENTITY traversal.
_MAP = {
    # nodes
    "prefers worked examples over abstract theory": _vec(0),
    "takes metformin daily": _vec(4),
    "managing my health and wellness": _vec(50),
    "annual checkup appointment": _vec(6),
    "prescribed new dosage adjustment": _vec(7),
    # queries
    "how do i learn best": _vec(0),
    "metformin": _vec(50),
    "annual checkup": _vec(6),
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


def _settings(backend: str, **overrides: object) -> GraphSettings:
    if backend == "turbovec":
        pytest.importorskip("turbovec")
        return GraphSettings(index_backend="turbovec", index_bit_width=4, **overrides)  # type: ignore[arg-type]
    return GraphSettings(**overrides)  # type: ignore[arg-type]


@pytest.fixture(params=["pgvector", "turbovec"])
def stack(request: pytest.FixtureRequest, _engine: Engine) -> Iterator[_Stack]:
    settings = _settings(request.param)
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    store = build_graph_store(
        engine=_engine,
        embedder=_Embedder(),
        audit_logger=MemoryAuditLogger(),
        settings=settings,
        persona_guard=False,
        node_releaser=None,
    )
    yield store, _engine
    with _engine.begin() as conn:
        graph_metadata.drop_all(conn)


def _retriever(store: object, **overrides: object) -> HybridRetriever:
    return HybridRetriever(store, GraphSettings(**overrides))  # type: ignore[arg-type]


# --- §3 paraphrase case + §4 no-gating (criterion 1/4/5) ----------------------


def test_paraphrase_match_is_dense_only_and_survives_fusion(stack: _Stack) -> None:
    store, _e = stack
    pref = store.merge("u1", _cand("prefers worked examples over abstract theory"))  # type: ignore[attr-defined]
    store.merge("u1", _cand("managing my health and wellness"))  # type: ignore[attr-defined]

    # The query shares no lexical tokens with the preference → FTS misses it.
    fts = store.search_fts("u1", "how do i learn best", top_k=10)  # type: ignore[attr-defined]
    assert pref.node_id not in {n.id for n in fts}

    out = _retriever(store).retrieve("u1", "how do i learn best")
    by_id = {r.node.id: r for r in out}
    assert pref.node_id in by_id  # survived fusion via the dense leg (criterion 5)
    assert by_id[pref.node_id].sparse_rank is None  # no-gating: dense-only, not gated out
    assert by_id[pref.node_id].dense_rank is not None


# --- §3 exact-term case (criterion 3/4) ---------------------------------------


def test_exact_term_recovered_by_sparse_when_dense_ranks_it_poorly(stack: _Stack) -> None:
    store, _e = stack
    metformin = store.merge("u1", _cand("takes metformin daily"))  # type: ignore[attr-defined]
    distractor = store.merge("u1", _cand("managing my health and wellness"))  # type: ignore[attr-defined]

    # Dense alone ranks the distractor above the exact-term node.
    dense = store.search_dense("u1", "metformin", top_k=10)  # type: ignore[attr-defined]
    assert dense[0].id == distractor.node_id

    # Sparse finds the exact term decisively.
    fts = store.search_fts("u1", "metformin", top_k=10)  # type: ignore[attr-defined]
    assert metformin.node_id in {n.id for n in fts}

    # Hybrid recovers what dense missed (criterion 4).
    out = {r.node.id: r for r in _retriever(store).retrieve("u1", "metformin")}
    assert metformin.node_id in out
    assert out[metformin.node_id].sparse_rank is not None


# --- allowlist: cross-owner isolation + the K4 subtraction (criterion 6) -------


def test_cross_owner_isolation_through_the_retriever(stack: _Stack) -> None:
    store, _e = stack
    store.merge("u1", _cand("takes metformin daily"))  # type: ignore[attr-defined]
    u2 = store.merge("u2", _cand("takes metformin daily"))  # type: ignore[attr-defined]
    out = _retriever(store).retrieve("u1", "metformin")
    assert u2.node_id not in {r.node.id for r in out}  # u2's node never crosses


def test_k4_subtraction_removes_flagged_node_from_the_sparse_leg(stack: _Stack) -> None:
    store, _e = stack
    metformin = store.merge("u1", _cand("takes metformin daily"))  # type: ignore[attr-defined]
    keep = store.merge("u1", _cand("managing my health and wellness"))  # type: ignore[attr-defined]

    # FTS WOULD surface the metformin node — but K4 flags it: allowlist excludes it.
    allow = {keep.node_id}  # user_nodes − {metformin}
    out = {r.node.id for r in _retriever(store).retrieve("u1", "metformin", allowlist=allow)}
    assert metformin.node_id not in out  # removed despite the sparse-leg hit
    assert keep.node_id in out  # no effect on the rest


# --- typed-link traversal (criterion 7) ---------------------------------------


def test_entity_thread_surfaces_a_node_the_flat_query_missed(stack: _Stack) -> None:
    store, engine = stack
    registry = PostgresEntityRegistry(
        backend=PostgresGraphBackend(engine=engine, persona_guard=False, node_releaser=None),
        embedder=_Embedder(),
    )
    ent = registry.create_entity("u1", canonical_name="Dr. Hansen")
    seed = store.merge("u1", _cand("annual checkup appointment", entity_ids=(ent.id,)))  # type: ignore[attr-defined]
    thread = store.merge("u1", _cand("prescribed new dosage adjustment", entity_ids=(ent.id,)))  # type: ignore[attr-defined]

    # The thread node is lexically + semantically disjoint from the query. In a
    # realistically large graph it falls outside the candidate pools; on this toy
    # graph we make that cutoff explicit with dense_pool=1 (the seed alone ranks).
    # FTS misses it too (no token overlap with "annual checkup").
    assert thread.node_id not in {n.id for n in store.search_dense("u1", "annual checkup", top_k=1)}  # type: ignore[attr-defined]
    assert thread.node_id not in {n.id for n in store.search_fts("u1", "annual checkup", top_k=10)}  # type: ignore[attr-defined]

    out = {r.node.id: r for r in _retriever(store, dense_pool=1).retrieve("u1", "annual checkup")}
    assert seed.node_id in out
    # via_traversal is True ONLY for a node the retriever did NOT get as a direct
    # hit (direct hits dedupe ahead of traversal) — so this proves the miss.
    assert thread.node_id in out
    assert out[thread.node_id].via_traversal is True
    assert out[thread.node_id].traversal_link_type is LinkType.ENTITY


# --- index-sync observable from the retrieval side (criterion 8) ---------------


def test_written_node_retrievable_then_deleted_node_gone(stack: _Stack) -> None:
    store, _e = stack
    node = store.merge("u1", _cand("takes metformin daily"))  # type: ignore[attr-defined]
    retriever = _retriever(store)

    assert node.node_id in {r.node.id for r in retriever.retrieve("u1", "metformin")}
    assert store.delete_node("u1", node.node_id) is True  # type: ignore[attr-defined]
    assert node.node_id not in {r.node.id for r in retriever.retrieve("u1", "metformin")}
