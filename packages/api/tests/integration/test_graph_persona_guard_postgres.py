"""A graph write holds the persona it names, so an erasure cannot race it (R9-284).

The coordinator's design ruling: every graph write that carries a persona id takes that
persona's row ``FOR KEY SHARE`` in the write's own transaction. The erasure's row delete then
waits for a writer that got there first, and a writer that comes after the delete finds no row
and is refused. Both directions are proved here on a migrated Postgres, as ``persona_app``
under row level security, which is the role the request path and the worker write with.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.errors import GraphPersonaGoneError
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_alice"
_PERSONA = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES (:o, 'a@x.test')"), {"o": _OWNER})
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _PERSONA, "o": _OWNER},
        )
    engine = make_rls_engine(app_url)
    token = current_user_id.set(_OWNER)
    yield engine
    current_user_id.reset(token)
    engine.dispose()


def _node(node_id: str) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"about {node_id}",
        provenance=(
            NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                persona_id=_PERSONA,
                written_at=_NOW,
                grounding="said it",
            ),
        ),
        created_at=_NOW,
    )


def _nodes(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM graph_nodes WHERE owner_id = :o"), {"o": _OWNER})
        return {str(r[0]) for r in rows}


def test_a_write_after_the_erasure_deleted_the_row_is_refused(
    app_engine: Engine,
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})
    backend = PostgresGraphBackend(engine=app_engine, persona_guard=True)

    with pytest.raises(GraphPersonaGoneError):
        backend.insert_node(_OWNER, _node("late"), embedder.encode(["late"])[0])

    assert _nodes(migrated_engine) == set()


def test_a_write_holding_the_row_makes_the_erasures_delete_wait(
    app_engine: Engine,
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    backend = PostgresGraphBackend(engine=app_engine, persona_guard=True)
    holding, release = threading.Event(), threading.Event()

    @event.listens_for(app_engine, "after_execute")
    def _pause_after_the_hold(_conn: Any, clause: Any, *_args: Any) -> None:  # noqa: ANN401
        if "FOR KEY SHARE" in str(clause) and not holding.is_set():
            holding.set()
            release.wait(timeout=60)

    errors: list[BaseException] = []

    def _write() -> None:
        token = current_user_id.set(_OWNER)
        try:
            backend.insert_node(_OWNER, _node("racing"), embedder.encode(["racing"])[0])
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)
        finally:
            current_user_id.reset(token)

    writer = threading.Thread(target=_write)
    writer.start()
    assert holding.wait(timeout=60), "the writer never took the persona's row"
    try:
        with migrated_engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            with pytest.raises(OperationalError, match="lock"):
                conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})
    finally:
        release.set()
        writer.join(timeout=60)

    assert errors == []
    assert _nodes(migrated_engine) == {"racing"}, "the writer that held the row committed"
    with migrated_engine.begin() as conn:  # and once it has, the delete goes through
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})
