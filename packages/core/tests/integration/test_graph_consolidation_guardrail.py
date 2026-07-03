"""The §0 structural guardrail for the consolidation pass (Spec K7, K7-D-11; acceptance 3).

Proves — over the REAL store surface, not mocks — that consolidation is additive,
provenance-safe, reversible, and LLM-free:

1. **No hard-delete:** across a pass (+ unmerge) every pre-existing node row stays
   present and the row count is monotone non-decreasing; assertion edges are only ever
   window-closed (never deleted) by the pass.
2. **No LLM-rewrite:** raw ``content`` byte-equality for every touched node (canonical
   AND member) across the pass; and a STRUCTURAL import assertion that the consolidation
   module pulls in no chat/LLM backend.
3. **Reversibility:** consolidate → unmerge → retrieval-equivalent (dense results
   identical).
4. **Idempotency:** the replay matrix — a second pass mutates nothing; a second unmerge
   is an honest no-op.
5. **K7-D-1.3 stays green through the pass:** the pass closes assertion edges ONLY with a
   ``consolidation:`` tag (re-openable), and hard-deletes none.
"""

from __future__ import annotations

import inspect
import math
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph import consolidation as consolidation_module
from persona.graph._schema import graph_edges, graph_metadata, graph_nodes
from persona.graph.consolidation import ConsolidationPass
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
    from collections.abc import Iterator, Sequence

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

DIM = 384
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


class _FakeIndex:
    def __init__(self) -> None:
        self.present: set[int] = set()

    def add(self, *, surrogate: int, vector: Sequence[float]) -> None:  # noqa: ARG002
        self.present.add(surrogate)

    def remove(self, surrogate: int) -> bool:
        self.present.discard(surrogate)
        return True


class _Embedder:
    model_name = "mapping"
    dimension = DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_vec(0) for _ in texts]


def _node(node_id: str, name: str) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.CONCEPT,
        concept_name=name,
        content=f"content of {name}",
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=T0),),
        created_at=T0,
    )


def _sem(src: str, dst: str, weight: float) -> TypedLink:
    return TypedLink(
        id=make_edge_id(src, dst, LinkType.SEMANTIC),
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.SEMANTIC,
        weight=weight,
        created_at=T0,
        valid_at=T0,
    )


def _temporal(src: str, dst: str) -> TypedLink:
    return TypedLink(
        id=make_edge_id(src, dst, LinkType.TEMPORAL),
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.TEMPORAL,
        provenance=NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=T0),
        created_at=T0,
        valid_at=T0,
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
def env(pg_engine: Engine) -> Iterator[tuple[PostgresGraphBackend, ConsolidationPass, Engine]]:
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    backend = PostgresGraphBackend(engine=pg_engine)
    pass_ = ConsolidationPass(
        backend=backend, index=_FakeIndex(), embedder=_Embedder(), audit_logger=MemoryAuditLogger()
    )
    yield backend, pass_, pg_engine
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)


C = "u1::node::00000000"
M = "u1::node::00000001"
X = "u1::node::00000002"


def _seed(backend: PostgresGraphBackend) -> None:
    backend.insert_node("u1", _node(C, "Canonical"), _vec(0))
    backend.insert_node("u1", _node(M, "Member"), _vec(0, 0.85))
    backend.insert_node("u1", _node(X, "External"), _vec(9))
    backend.upsert_edge("u1", _sem(C, M, 0.85))
    backend.upsert_edge("u1", _temporal(M, X))


def _counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as conn:
        nodes = conn.execute(select(func.count()).select_from(graph_nodes)).scalar_one()
        edges = conn.execute(select(func.count()).select_from(graph_edges)).scalar_one()
    return int(nodes), int(edges)


# ----- 1. no hard-delete; row counts monotone (K7-D-11.1) ------------------


def test_no_hard_delete_rows_monotone(
    env: tuple[PostgresGraphBackend, ConsolidationPass, Engine],
) -> None:
    backend, pass_, engine = env
    _seed(backend)
    nodes_before, edges_before = _counts(engine)
    pass_.run("u1")
    nodes_after, edges_after = _counts(engine)
    # nodes: NONE deleted (monotone non-decreasing). edges: the member's edge is
    # window-closed (still a row) + a mirror added → count only grows.
    assert nodes_after == nodes_before
    assert edges_after >= edges_before
    # the member's assertion edge survives as a CLOSED row (not deleted).
    with engine.connect() as conn:
        closed = conn.execute(
            select(func.count())
            .select_from(graph_edges)
            .where(
                graph_edges.c.id == make_edge_id(M, X, LinkType.TEMPORAL),
                graph_edges.c.invalid_at.isnot(None),
            )
        ).scalar_one()
    assert closed == 1


# ----- 2a. content byte-equality across the pass (K7-D-11.2) ---------------


def test_content_byte_equality_across_pass(
    env: tuple[PostgresGraphBackend, ConsolidationPass, Engine],
) -> None:
    backend, pass_, _engine = env
    _seed(backend)
    before = {n: backend.get_node("u1", n).content for n in (C, M)}  # type: ignore[union-attr]
    pass_.run("u1")
    after = {n: backend.get_node("u1", n).content for n in (C, M)}  # type: ignore[union-attr]
    assert after == before  # canonical AND member content untouched (no LLM rewrite)


# ----- 2b. structural: no LLM/chat backend import (K7-D-11.2) ---------------


def test_consolidation_imports_no_llm_backend() -> None:
    src = inspect.getsource(consolidation_module)
    forbidden = ("persona.backends", "persona.chat", "completion", "anthropic", "openai", "litellm")
    for token in forbidden:
        assert token not in src, f"consolidation must be LLM-free but references {token!r}"


# ----- 3. reversibility: consolidate → unmerge → retrieval-equivalent -------


def test_consolidate_unmerge_retrieval_equivalent(
    env: tuple[PostgresGraphBackend, ConsolidationPass, Engine],
) -> None:
    backend, pass_, _engine = env
    _seed(backend)
    before = [(n.id, n.content) for n in backend.dense_query("u1", _vec(0), top_k=100)]
    pass_.run("u1")
    assert pass_.unmerge("u1", M) is True
    after = [(n.id, n.content) for n in backend.dense_query("u1", _vec(0), top_k=100)]
    assert after == before  # retrieval-equivalent to pre-merge


# ----- 4. idempotency replay matrix (K7-D-11.4) ----------------------------


def test_idempotency_replay_matrix(
    env: tuple[PostgresGraphBackend, ConsolidationPass, Engine],
) -> None:
    backend, pass_, engine = env
    _seed(backend)
    pass_.run("u1")
    state = _counts(engine)
    # second pass: no new mutation.
    second = pass_.run("u1")
    assert second.nodes_merged == 0
    assert _counts(engine) == state
    # unmerge, then a second unmerge is an honest no-op.
    assert pass_.unmerge("u1", M) is True
    assert pass_.unmerge("u1", M) is False


# ----- 5. K7-D-1.3 stays green: only consolidation-tagged closes ------------


def test_pass_closes_only_with_consolidation_tag(
    env: tuple[PostgresGraphBackend, ConsolidationPass, Engine],
) -> None:
    backend, pass_, engine = env
    _seed(backend)
    pass_.run("u1")
    with engine.connect() as conn:
        # every closed edge was closed BY consolidation (tagged), never silently.
        untagged = conn.execute(
            select(func.count())
            .select_from(graph_edges)
            .where(
                graph_edges.c.invalid_at.isnot(None),
                ~graph_edges.c.invalidated_by.like("consolidation:%"),
            )
        ).scalar_one()
    assert untagged == 0
