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
from persona.graph.models import ConceptNode, LinkType, NodeKind, NodeProvenance, TypedLink
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


def _edge(edge_id: str, src: str, dst: str) -> TypedLink:
    return TypedLink(
        id=edge_id,
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.CAUSAL,
        provenance=NodeProvenance(
            source=WriteSource.PERSONA_SELF, persona_id=_PERSONA, written_at=_NOW
        ),
        created_at=_NOW,
        valid_at=_NOW,
    )


@pytest.mark.parametrize("table", ["graph_nodes", "graph_edges"])
def test_the_row_stays_held_until_the_write_commits(
    app_engine: Engine,
    migrated_engine: Engine,
    embedder: Any,  # noqa: ANN401
    table: str,
) -> None:
    """The hold and the write are one transaction: the delete waits past the write itself.

    The writer is paused after its INSERT has run and before it commits. Nothing in the graph
    references ``personas`` by foreign key, so only the hold, still open in that same
    transaction, can make the delete wait here; a hold committed before the write (its own
    transaction) would already be gone, and the delete would go through.
    """
    backend = PostgresGraphBackend(engine=app_engine, persona_guard=True)
    vector = embedder.encode(["x"])[0]
    if table == "graph_edges":
        for node_id in ("a", "b"):
            backend.insert_node(_OWNER, _node(node_id), vector)

    def _write() -> None:
        if table == "graph_nodes":
            backend.insert_node(_OWNER, _node("racing"), vector)
        else:
            backend.upsert_edge(_OWNER, _edge("e_racing", "a", "b"))

    written, release = threading.Event(), threading.Event()

    @event.listens_for(app_engine, "after_execute")
    def _pause_after_the_write(_conn: Any, clause: Any, *_args: Any) -> None:  # noqa: ANN401
        is_the_write = str(clause).lstrip().upper().startswith(f"INSERT INTO {table.upper()}")
        if is_the_write and not written.is_set():
            written.set()
            release.wait(timeout=60)

    errors: list[BaseException] = []

    def _run() -> None:
        token = current_user_id.set(_OWNER)
        try:
            _write()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)
        finally:
            current_user_id.reset(token)

    writer = threading.Thread(target=_run)
    writer.start()
    assert written.wait(timeout=60), "the writer never wrote"
    try:
        with migrated_engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '1s'"))
            with pytest.raises(OperationalError, match="lock"):
                conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})
    finally:
        release.set()
        writer.join(timeout=60)

    assert errors == []
    if table == "graph_nodes":
        assert "racing" in _nodes(migrated_engine), "the writer that held the row committed"
    with migrated_engine.begin() as conn:  # once the write has committed, the delete goes through
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})


def test_a_record_that_fails_after_the_insert_lands_no_link(app_engine: Engine) -> None:
    """Reviewer's M22: the links and the record are one transaction on Postgres too."""
    from persona.graph.models import CanonicalEntity  # noqa: PLC0415

    backend = PostgresGraphBackend(engine=app_engine, persona_guard=True)
    backend.insert_node(_OWNER, _node("r_k"), [0.0] * 384)
    backend.insert_entity(
        _OWNER, CanonicalEntity(id="r_ent", canonical_name="r_ent", created_at=_NOW), [0.0] * 384
    )

    def _broken(_added: list[str]) -> tuple[dict[str, str], str]:
        raise RuntimeError("the record could not be built")

    with pytest.raises(RuntimeError, match="could not be built"):
        backend.associate_entities_recording(
            _OWNER,
            "r_k",
            ["r_ent"],
            member_id="r_m",
            merged_into="r_k",
            expected_content_hash="h",
            expected_provenance=(),
            record=_broken,
            guard_personas=[_PERSONA],
        )

    assert backend.entities_for_node(_OWNER, "r_k") == []
