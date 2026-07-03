"""Integration tests for the Spec K7 T6 evidence-salience dynamics over real Postgres.

Proves against real SQL: corroboration (extend) rises + contradiction (supersede)
falls on the merge path; absorption corroborates the canonical + disuse decays idle
kinds on the consolidation pass; a two-year-equivalent idle TRAIT stays undiminished;
``record_recall`` reinforces (fail-soft, off the token path); clamping holds; salience
moves NO retrieval result (v1 gates nothing on it); and the whole thing is time-shift
invariant (epochs, never timestamps).
"""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph._schema import graph_metadata
from persona.graph.config import GraphSettings
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
from persona.graph.protocol import KnowledgeCandidate, UpdateIntent
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

DIM = 384
S = GraphSettings()


def _vec(primary: int, cos: float = 1.0, secondary: int = 383) -> list[float]:
    v = [0.0] * DIM
    v[primary] = cos
    v[secondary] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


class _FakeIndex:
    def add(self, *, surrogate: int, vector: Sequence[float]) -> None:  # noqa: ARG002
        pass

    def remove(self, surrogate: int) -> bool:  # noqa: ARG002
        return True


_MAP = {
    "lives in Bergen": _vec(0),
    "lives in Oslo": _vec(5),
    "loves espresso": _vec(0, 0.95, 1),  # cos 0.95 → extends Bergen? no; use for extend
}


class _Embedder:
    model_name = "mapping"
    dimension = DIM

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            if t in _MAP:
                out.append(_MAP[t])
                continue
            keys = [k for k in _MAP if k in t]
            out.append(_MAP[max(keys, key=len)] if keys else _vec(100))
        return out


def _prov(written_at: datetime) -> NodeProvenance:
    return NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=written_at)


def _node(nid: str, name: str, content: str, *, kind: NodeKind, created: datetime) -> ConceptNode:
    return ConceptNode(
        id=nid,
        node_kind=kind,
        concept_name=name,
        content=content,
        provenance=(_prov(created),),
        created_at=created,
    )


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
def env(pg_engine: Engine):  # noqa: ANN201
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)
        graph_metadata.create_all(conn)
    backend = PostgresGraphBackend(engine=pg_engine)
    store = build_graph_store(
        engine=pg_engine, embedder=_Embedder(), audit_logger=MemoryAuditLogger()
    )
    pass_ = ConsolidationPass(
        backend=backend,
        index=_FakeIndex(),
        embedder=_Embedder(),
        audit_logger=MemoryAuditLogger(),
        settings=S,
    )
    yield backend, store, pass_
    with pg_engine.begin() as conn:
        graph_metadata.drop_all(conn)


T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _cand(content: str, **kw: object) -> KnowledgeCandidate:
    base: dict[str, object] = {
        "concept_name": content[:12],
        "content": content,
        "node_kind": NodeKind.FACT,
        "provenance": _prov(T0),
    }
    base.update(kw)
    return KnowledgeCandidate(**base)  # type: ignore[arg-type]


# ----- merge path: corroboration rises, contradiction falls ----------------


def test_extend_corroborates_supersede_contradicts(env) -> None:  # noqa: ANN001
    backend, store, _pass = env
    created = store.merge("u1", _cand("lives in Bergen"))
    assert backend.get_salience("u1", created.node_id) == (
        S.salience_default,
        0,
    )  # CREATED: no bump

    store.merge("u1", _cand("loves espresso"))  # cos 0.95 → EXTENDED → +δ_c
    sal, _ = backend.get_salience("u1", created.node_id)  # type: ignore[misc]
    assert sal == pytest.approx(S.salience_default + S.salience_delta_corroboration)

    # a later supersede → EVOLVED → −δ_x.
    store.merge(
        "u1",
        _cand(
            "lives in Oslo",
            provenance=_prov(T0 + timedelta(days=30)),
            update_intent=UpdateIntent.CONTRADICT,
            target_node_id=created.node_id,
        ),
    )
    sal2, _ = backend.get_salience("u1", created.node_id)  # type: ignore[misc]
    assert sal2 == pytest.approx(
        S.salience_default + S.salience_delta_corroboration - S.salience_delta_contradiction
    )


# ----- consolidation: absorption corroborates + disuse decays --------------


def test_absorption_corroborates_and_disuse_decays(env) -> None:  # noqa: ANN001
    backend, _store, pass_ = env
    c, m = "u1::node::00000000", "u1::node::00000001"
    backend.insert_node(
        "u1", _node(c, "can", "canonical", kind=NodeKind.CONCEPT, created=T0), _vec(0)
    )
    backend.insert_node(
        "u1", _node(m, "mem", "member", kind=NodeKind.CONCEPT, created=T0), _vec(0, 0.85)
    )
    circ = "u1::node::00000002"
    backend.insert_node(
        "u1", _node(circ, "c", "circumstance", kind=NodeKind.CIRCUMSTANCE, created=T0), _vec(30)
    )
    backend.upsert_edge(
        "u1",
        TypedLink(
            id=make_edge_id(c, m, LinkType.SEMANTIC),
            src_node_id=c,
            dst_node_id=m,
            link_type=LinkType.SEMANTIC,
            weight=0.85,
            created_at=T0,
            valid_at=T0,
        ),
    )

    # run the pass enough times to advance past CIRCUMSTANCE grace + decay.
    grace = S.disuse_grace_for(str(NodeKind.CIRCUMSTANCE))
    for _ in range(grace + 2):
        pass_.run("u1")

    # the canonical absorbed the member → corroborated (>= default; merged only run 1).
    can_sal, _ = backend.get_salience("u1", c)  # type: ignore[misc]
    assert can_sal >= S.salience_default + S.salience_delta_corroboration
    # the idle CIRCUMSTANCE decayed below default.
    circ_sal, _ = backend.get_salience("u1", circ)  # type: ignore[misc]
    assert circ_sal < S.salience_default


def test_two_year_trait_undiminished(env) -> None:  # noqa: ANN001
    backend, _store, pass_ = env
    trait = "u1::node::00000000"
    backend.insert_node(
        "u1", _node(trait, "t", "identity trait", kind=NodeKind.TRAIT, created=T0), _vec(0)
    )
    # ~two years of idle consolidation passes (epochs) — no contradicting evidence.
    for _ in range(24):
        pass_.run("u1")
    sal, _ = backend.get_salience("u1", trait)  # type: ignore[misc]
    assert sal == S.salience_default  # a TRAIT never fades by disuse (K7-D-6 acceptance)


# ----- record_recall: reinforces, fail-soft, SELF-excluded -----------------


def test_record_recall_reinforces_and_is_fail_soft(env) -> None:  # noqa: ANN001
    backend, store, _pass = env
    created = store.merge("u1", _cand("lives in Bergen"))
    store.record_recall("u1", [created.node_id])
    sal, _ = backend.get_salience("u1", created.node_id)  # type: ignore[misc]
    assert sal == pytest.approx(S.salience_default + S.salience_delta_recall)
    # fail-soft: an unknown id never raises.
    store.record_recall("u1", ["u1::node::00099999"])  # no such node → silent no-op


# ----- salience moves NO retrieval result (v1 gates nothing on it) ----------


def test_salience_does_not_change_retrieval_order(env) -> None:  # noqa: ANN001
    backend, store, _pass = env
    a = store.merge("u1", _cand("lives in Bergen"))  # _vec(0)
    b = store.merge("u1", _cand("lives in Oslo"))  # _vec(5)
    before = [n.id for n in backend.dense_query("u1", _vec(0), top_k=10)]
    # heavily reinforce b's salience; retrieval order must not change (distance-only).
    for _ in range(10):
        store.record_recall("u1", [b.node_id])
    after = [n.id for n in backend.dense_query("u1", _vec(0), top_k=10)]
    assert after == before
    assert before[0] == a.node_id  # nearest to _vec(0) is Bergen, regardless of salience


# ----- time-shift invariance: epochs, never timestamps ---------------------


def test_time_shift_invariance(env) -> None:  # noqa: ANN001
    backend, store, _pass = env
    # sequence 1 at T0.
    n1 = store.merge("u1", _cand("lives in Bergen", provenance=_prov(T0)))
    store.merge("u1", _cand("loves espresso", provenance=_prov(T0)))  # extend → +δ_c
    s1, _ = backend.get_salience("u1", n1.node_id)  # type: ignore[misc]

    # the IDENTICAL sequence for a different owner, timestamps shifted +2 years.
    shift = T0 + timedelta(days=730)
    n2 = store.merge("u2", _cand("lives in Bergen", provenance=_prov(shift)))
    store.merge("u2", _cand("loves espresso", provenance=_prov(shift)))
    s2, _ = backend.get_salience("u2", n2.node_id)  # type: ignore[misc]

    assert s1 == s2  # salience depends on the event log + epoch, never the timestamp
