"""Consolidation and the erasure's graph step never interleave (D-R9-284-graph-mutex).

On a migrated Postgres: the graph step waits while a consolidation holds the owner's lock, and
the two interleavings review round 5 found (an edge's asserter erased after the merge record,
and a merge working from a canonical read before an erasure) leave nothing of the erased
persona once both have finished.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from _graph_versions import snapshot_node_version
from persona.audit import MemoryAuditLogger
from persona.graph.config import GraphSettings
from persona.graph.consolidation import ConsolidationPass
from persona.graph.errors import GraphPersonaGoneError
from persona.graph.index import make_graph_index
from persona.graph.models import ConceptNode, LinkType, NodeKind, NodeProvenance, TypedLink
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource
from persona_api.services.erasure_graph import erase_persona_graph
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_mutex"
_P = "persona_1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"
_Q = "persona_2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b"
_NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def _by(persona_id: str) -> NodeProvenance:
    return NodeProvenance(source=WriteSource.SYSTEM, persona_id=persona_id, written_at=_NOW)


def _node(node_id: str, *by: str) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"about {node_id}",
        provenance=tuple(_by(p) for p in by),
        created_at=_NOW,
    )


def _edge(src: str, dst: str, by: str) -> TypedLink:
    return TypedLink(
        id=f"{src}::causal::{dst}",
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.CAUSAL,
        provenance=_by(by),
        created_at=_NOW,
        valid_at=_NOW,
    )


@pytest.fixture
def backend(migrated_engine: Engine) -> PostgresGraphBackend:
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'm@x.test')"), {"o": _OWNER})
        for pid in (_P, _Q):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    return PostgresGraphBackend(engine=migrated_engine, persona_guard=True)


def _pass(engine: Engine, backend: PostgresGraphBackend, embedder: Any) -> ConsolidationPass:  # noqa: ANN401
    settings = GraphSettings()
    return ConsolidationPass(
        backend=backend,
        index=make_graph_index(
            settings=settings, engine=engine, float32_fetch=backend.embeddings_by_surrogate
        ),
        embedder=embedder,
        audit_logger=MemoryAuditLogger(),
        settings=settings,
    )


def _naming(engine: Engine, persona_id: str) -> int:
    """Rows anywhere in the owner's graph that still name the persona."""
    params = {"o": _OWNER, "p": persona_id}
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT (SELECT count(*) FROM graph_nodes WHERE owner_id = :o AND "
                    "  provenance @> jsonb_build_array(jsonb_build_object('persona_id', "
                    "  CAST(:p AS text)))) "
                    "+ (SELECT count(*) FROM graph_node_versions WHERE owner_id = :o AND "
                    "  provenance @> jsonb_build_array(jsonb_build_object('persona_id', "
                    "  CAST(:p AS text)))) "
                    "+ (SELECT count(*) FROM graph_edges WHERE owner_id = :o AND "
                    "  provenance @> jsonb_build_object('persona_id', CAST(:p AS text)))"
                ),
                params,
            ).scalar_one()
        )


def _open_mirrors(engine: Engine, run_id: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id FROM graph_edges WHERE owner_id = :o AND invalid_at IS NULL "
                "AND provenance @> jsonb_build_object('reason', CAST(:r AS text))"
            ),
            {"o": _OWNER, "r": f"consolidation:{run_id}"},
        )
        return {str(r[0]) for r in rows}


def _erase_in_thread(engine: Engine, persona_id: str) -> tuple[threading.Thread, list[object]]:
    """Delete the persona's row (as the erasure's row step does), then run its graph step."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": persona_id})
    outcome: list[object] = []

    def _run() -> None:
        try:
            outcome.append(erase_persona_graph(engine, owner_id=_OWNER, persona_id=persona_id))
        except Exception as exc:  # noqa: BLE001 - reported to the test
            outcome.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, outcome


def _wrap_after(target: object, name: str, then: Callable[[], None]) -> None:
    """Run ``then`` once, right after the first call of ``target.name``."""
    real = getattr(target, name)
    done: list[bool] = []

    def _wrapped(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        result = real(*args, **kwargs)
        if not done:
            done.append(True)
            then()
        return result

    setattr(target, name, _wrapped)


def test_the_erasure_graph_step_waits_while_a_consolidation_holds_the_lock(
    migrated_engine: Engine, backend: PostgresGraphBackend
) -> None:
    timed = create_engine(migrated_engine.url)

    @event.listens_for(timed, "begin")
    def _short_wait(conn: Any) -> None:  # noqa: ANN401
        conn.exec_driver_sql("SET LOCAL lock_timeout = '1s'")

    try:
        with backend.maintenance_lock(_OWNER), pytest.raises(OperationalError, match="lock"):
            erase_persona_graph(timed, owner_id=_OWNER, persona_id=_P)
        assert isinstance(erase_persona_graph(timed, owner_id=_OWNER, persona_id=_P), dict)
    finally:
        timed.dispose()


def test_an_edges_asserter_erased_after_the_merge_record_leaves_no_mirror(
    migrated_engine: Engine,
    backend: PostgresGraphBackend,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Round 5 H1: m is P's, but its edge m->x is Q's; Q is erased after the record."""
    vector = embedder.encode(["x"])[0]
    for node in (_node("h_k", _P), _node("h_m", _P), _node("h_x", _P)):
        backend.insert_node(_OWNER, node, vector)
    backend.upsert_edge(_OWNER, _edge("h_m", "h_x", _Q))
    consolidation = _pass(migrated_engine, backend, embedder)
    erasing: list[tuple[threading.Thread, list[object]]] = []
    _wrap_after(
        backend, "set_merged_into", lambda: erasing.append(_erase_in_thread(migrated_engine, _Q))
    )

    with backend.maintenance_lock(_OWNER):
        with pytest.raises(GraphPersonaGoneError):
            consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
                _OWNER,
                backend.get_node(_OWNER, "h_k"),
                [backend.get_node(_OWNER, "h_m")],
                run_id="rh1",
                now=_NOW,
                epoch=1,
            )
        ((thread, outcome),) = erasing
        thread.join(timeout=1.0)
        assert thread.is_alive(), "the graph step waits for the consolidation"
    thread.join(timeout=30.0)

    assert isinstance(outcome[0], dict), outcome
    assert _open_mirrors(migrated_engine, "rh1") == set()
    assert _naming(migrated_engine, _Q) == 0


def test_a_merge_from_a_canonical_read_before_an_erasure_leaves_nothing_of_it(
    migrated_engine: Engine,
    backend: PostgresGraphBackend,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Round 5 H3: the canonical (P's and Q's) was read, then P was erased, then the merge ran."""
    vector = embedder.encode(["x"])[0]
    for node in (_node("s_k", _P, _Q), _node("s_m", _Q), _node("s_x", _Q)):
        backend.insert_node(_OWNER, node, vector)
    backend.upsert_edge(_OWNER, _edge("s_m", "s_x", _Q))
    snapshot_node_version(
        migrated_engine, _OWNER, "s_k", valid_at=_NOW, invalid_at=_NOW, invalidated_by=None
    )
    consolidation = _pass(migrated_engine, backend, embedder)

    with backend.maintenance_lock(_OWNER):
        stale_canonical = backend.get_node(_OWNER, "s_k")
        member = backend.get_node(_OWNER, "s_m")
        thread, outcome = _erase_in_thread(migrated_engine, _P)
        thread.join(timeout=1.0)
        assert thread.is_alive(), "the graph step waits for the consolidation"
        with pytest.raises(GraphPersonaGoneError):  # the alias accretion names P's trail
            consolidation._merge_cluster(  # noqa: SLF001 - the merge act itself
                _OWNER, stale_canonical, [member], run_id="rh3", now=_NOW, epoch=1
            )
    thread.join(timeout=30.0)

    assert isinstance(outcome[0], dict), outcome
    assert _naming(migrated_engine, _P) == 0
    canonical = backend.get_node(_OWNER, "s_k")
    assert canonical is not None
    assert {p.persona_id for p in canonical.provenance} == {_Q}


def test_the_sweep_returns_promptly_while_a_consolidation_holds_the_lock(
    migrated_engine: Engine, backend: PostgresGraphBackend
) -> None:
    """Round 6 M-3: the sweep runs on the worker's event loop; it defers the owner, not waits."""
    import time  # noqa: PLC0415
    from datetime import timedelta  # noqa: PLC0415

    from persona_api.jobs.handlers.persona_erase import PersonaEraseSweep  # noqa: PLC0415

    sweep = PersonaEraseSweep(
        engine=migrated_engine,
        grace=timedelta(0),
        run=lambda _e, _t: None,
        find_leftovers=lambda _engine, **_kw: [(_OWNER, _P)],
    )
    with backend.maintenance_lock(_OWNER):
        started = time.perf_counter()
        sweep()
        elapsed = time.perf_counter() - started

    assert elapsed < 5.0, f"the sweep waited {elapsed:.1f}s"
    assert sweep._retry == {(_OWNER, _P)}  # noqa: SLF001 - deferred to the next sweep
