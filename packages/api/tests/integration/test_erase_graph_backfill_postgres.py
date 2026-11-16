"""Migration 063: a persona deleted before R9-284 is taken out of the graph (review HIGH-2).

Such a persona has no ledger row, so the sweep's backstop never looked for it, and the write
guard refused the user's own correction of a node whose trail still named it. The migration
records an erasure for it; the next sweep then removes it, and the correction goes through.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph.errors import GraphPersonaGoneError
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.graph.store import build_graph_store
from persona.schema.chunks import WriteSource
from persona_api.jobs.handlers.persona_erase import PersonaEraseSweep
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "063_graph_erasure_backfill.py"
)
_OWNER = "user_backfill"
_GONE = "persona_0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c"
_KEPT = "persona_0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d0d"
_NOW = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)


def _backfill_sql() -> str:
    spec = importlib.util.spec_from_file_location("backfill_063", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.BACKFILL)


def _node(node_id: str, *by: str) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"about {node_id}",
        provenance=tuple(
            NodeProvenance(source=WriteSource.SYSTEM, persona_id=p, written_at=_NOW) for p in by
        ),
        created_at=_NOW,
    )


def _erasures(engine: Engine) -> list[tuple[str, str, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT owner_id, subject_id, state FROM erasures ORDER BY subject_id")
        ).all()
    return [(str(o), str(s), str(st)) for o, s, st in rows]


def test_a_persona_deleted_before_r9_284_is_backfilled_and_swept(
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'b@x.test')"), {"o": _OWNER})
        for pid in (_GONE, _KEPT):
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": pid, "o": _OWNER},
            )
    backend = PostgresGraphBackend(engine=migrated_engine, persona_guard=False)
    vector = embedder.encode(["x"])[0]
    backend.insert_node(_OWNER, _node("bf_shared", _GONE, _KEPT), vector)
    backend.insert_node(_OWNER, _node("bf_only", _GONE), vector)
    with migrated_engine.begin() as conn:  # the pre-R9-284 delete: the graph is left alone
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _GONE})
    store = build_graph_store(
        engine=migrated_engine,
        embedder=embedder,
        audit_logger=MemoryAuditLogger(),
        persona_guard=True,
    )
    with pytest.raises(GraphPersonaGoneError):
        store.correct_node(_OWNER, "bf_shared", "the user's own correction")

    sql = _backfill_sql()
    for _ in range(2):  # idempotent
        with migrated_engine.begin() as conn:
            conn.execute(text(sql))

    assert _erasures(migrated_engine) == [(_OWNER, _GONE, "complete")], "the live one is not"

    sweep = PersonaEraseSweep(engine=migrated_engine, grace=timedelta(0), run=lambda _e, _t: None)
    sweep()

    assert backend.get_node(_OWNER, "bf_only") is None
    shared = backend.get_node(_OWNER, "bf_shared")
    assert shared is not None
    assert {p.persona_id for p in shared.provenance} == {_KEPT}
    store.correct_node(_OWNER, "bf_shared", "the user's own correction")
    corrected = backend.get_node(_OWNER, "bf_shared")
    assert corrected is not None
    assert "the user's own correction" in corrected.content


def test_a_role_row_level_security_would_blind_is_refused(migrated_engine: Engine) -> None:
    """Round 5 LOW-4: persona_app sees only its own rows, so the backfill refuses to run as it."""
    import os  # noqa: PLC0415

    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.exc import DBAPIError  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("backfill_063", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with migrated_engine.begin() as conn:  # the superuser: passes
        conn.execute(text(module.ROLE_CHECK))

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    app = create_engine(app_url)
    try:
        with pytest.raises(DBAPIError, match="would not see every row"), app.begin() as conn:
            conn.execute(text(module.ROLE_CHECK))
    finally:
        app.dispose()
