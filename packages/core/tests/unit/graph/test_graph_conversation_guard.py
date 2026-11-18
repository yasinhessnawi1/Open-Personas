"""A graph write that cites a conversation holds that conversation's row first (R9-304).

Forgetting a conversation's memory takes its facts out of the graph in the transaction that
deletes the conversation's row. A synthesis already past its read can still be in its model
call then, and the graph is owner scoped, so nothing else stops its write from putting a fact
from the forgotten conversation back. With the guard on, every write that adds a provenance
entry citing a conversation runs ``SELECT 1 FROM conversations ... FOR KEY SHARE`` for it, in
the write's own transaction and before the write: the forget's row lock waits for a write in
flight (and then removes what it wrote), and a write after the delete finds no row and is
refused. Only the entries a write adds are held: an older entry naming a conversation deleted
without forgetting its memory must not make the node unwritable. Proved on Postgres in
packages/api's test_forget_conversation_graph_postgres.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.errors import GraphInteractionGoneError
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

_NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
_C = "conv-forgotten"
_D = "conv-kept"
_VECTOR = [0.0] * 384


def _from(conversation: str | None, kind: str | None = "conversation") -> NodeProvenance:
    return NodeProvenance(
        source=WriteSource.PERSONA_SELF,
        interaction_id=conversation,
        interaction_kind=kind if conversation else None,
        written_at=_NOW,
    )


def _node(*entries: NodeProvenance) -> ConceptNode:
    return ConceptNode(
        id="n1",
        node_kind=NodeKind.FACT,
        concept_name="n1",
        content="about n1",
        provenance=entries,
        created_at=_NOW,
    )


def _edge(entry: NodeProvenance | None) -> TypedLink:
    return TypedLink(
        id="e1",
        src_node_id="a",
        dst_node_id="b",
        link_type=LinkType.CAUSAL,
        provenance=entry,
        created_at=_NOW,
    )


def _row(entries: tuple[NodeProvenance, ...]) -> dict[str, Any]:
    """A ``graph_nodes`` row as supersede_node reads it, with ``entries`` as its trail."""
    return {
        "node_kind": "fact",
        "concept_name": "n1",
        "content": "about n1",
        "metadata": {},
        "wellbeing_category": None,
        "embedding": _VECTOR,
        "embedding_model": "m",
        "content_hash": "h",
        "provenance": [p.model_dump(mode="json") for p in entries],
    }


class _Result:
    def __init__(self, row: object = (1,), mapping: dict[str, Any] | None = None) -> None:
        self._row = row
        self._mapping = mapping
        self.rowcount = 1

    def first(self) -> object:
        return self._row

    def scalar_one(self) -> int:
        return 1

    def scalar_one_or_none(self) -> int | None:
        return 1

    def mappings(self) -> _Result:
        return self

    def one_or_none(self) -> dict[str, Any] | None:
        return self._mapping


class _Conn:
    """One transaction: every statement is logged with this transaction's number."""

    def __init__(self, engine: _Engine, txn: int) -> None:
        self._engine = engine
        self._txn = txn

    def execute(self, stmt: Any, params: dict[str, str] | None = None) -> _Result:  # noqa: ANN401
        sql = str(stmt)
        if "FROM conversations" in sql and "FOR KEY SHARE" in sql:
            assert params is not None
            self._engine.log.append((self._txn, f"hold {params['c']}"))
            return _Result(None if params["c"] in self._engine.gone else (1,))
        if "FOR KEY SHARE" in sql and "FROM personas" in sql:
            return _Result()
        verb = sql.split()[0].lower()
        self._engine.log.append((self._txn, verb))
        if verb == "select":
            return _Result(mapping=_row(self._engine.stored))
        return _Result()


class _Engine:
    def __init__(
        self, gone: frozenset[str] = frozenset(), stored: tuple[NodeProvenance, ...] = ()
    ) -> None:
        self.gone = set(gone)
        self.stored = stored
        self.log: list[tuple[int, str]] = []
        self._next = 0

    def _open(self) -> _Conn:
        self._next += 1
        return _Conn(self, self._next)

    @contextmanager
    def begin(self) -> Iterator[_Conn]:
        yield self._open()

    @contextmanager
    def connect(self) -> Iterator[_Conn]:
        yield self._open()

    def entries(self) -> list[str]:
        return [entry for _, entry in self.log]

    def holds(self) -> list[str]:
        return [entry for entry in self.entries() if entry.startswith("hold")]


def _backend(engine: _Engine, *, guard: bool = True) -> PostgresGraphBackend:
    return PostgresGraphBackend(engine=engine, persona_guard=guard, node_releaser=None)  # type: ignore[arg-type]


_WRITES = {
    "insert_node": (lambda b: b.insert_node("u1", _node(_from(_C)), _VECTOR), "insert"),
    "insert_node_if_absent": (
        lambda b: b.insert_node_if_absent("u1", _node(_from(_C)), _VECTOR),
        "insert",
    ),
    "update_node": (
        lambda b: b.update_node(
            "u1",
            _node(_from(_D), _from(_C)),
            _VECTOR,
            expected_content_hash="h",
            expected_provenance=(_from(_D),),
        ),
        "update",
    ),
    "supersede_node": (
        lambda b: b.supersede_node(
            "u1",
            _node(_from(_D), _from(_C)),
            _VECTOR,
            expected_content_hash="h",
            expected_provenance=(_from(_D),),
            valid_at=_NOW,
            invalid_at=_NOW,
            invalidated_by=None,
        ),
        "insert",
    ),
    "upsert_edge": (lambda b: b.upsert_edge("u1", _edge(_from(_C))), "insert"),
    "insert_edge_if_absent": (
        lambda b: b.insert_edge_if_absent("u1", _edge(_from(_C))),
        "insert",
    ),
    "append_provenance": (
        lambda b: b.append_provenance(
            "u1", "n1", _from(_C), expected_content_hash="h", expected_provenance=(_from(_D),)
        ),
        "update",
    ),
    "insert_entity": (
        lambda b: b.insert_entity(
            "u1",
            CanonicalEntity(id="x", canonical_name="x", provenance=_from(_C), created_at=_NOW),
            _VECTOR,
        ),
        "insert",
    ),
}


def _stored_for(name: str) -> tuple[NodeProvenance, ...]:
    """What the row holds before the write: the trail the CAS writes expect."""
    return (_from(_D),) if name == "supersede_node" else ()


@pytest.mark.parametrize("name", list(_WRITES))
def test_a_write_citing_a_conversation_holds_it_in_its_own_transaction_first(name: str) -> None:
    write, verb = _WRITES[name]
    engine = _Engine(stored=_stored_for(name))

    write(_backend(engine))

    (txn,) = {t for t, entry in engine.log if entry == verb}
    in_the_write = [entry for t, entry in engine.log if t == txn]
    assert f"hold {_C}" in in_the_write, f"{name}: the hold is not in the write's transaction"
    assert in_the_write.index(f"hold {_C}") < in_the_write.index(verb), f"{name}: write first"


@pytest.mark.parametrize("name", list(_WRITES))
def test_a_write_citing_a_forgotten_conversation_writes_nothing(name: str) -> None:
    write, _verb = _WRITES[name]
    engine = _Engine(gone=frozenset({_C}), stored=_stored_for(name))

    with pytest.raises(GraphInteractionGoneError) as raised:
        write(_backend(engine))

    assert [e for e in engine.entries() if e in {"insert", "update"}] == [], "nothing written"
    assert raised.value.context == {"interaction_id": _C}


def test_only_the_entries_a_write_adds_are_held() -> None:
    """An entry already on the node may name a conversation deleted without forgetting it."""
    engine = _Engine(gone=frozenset({_D}))

    _backend(engine).update_node(
        "u1",
        _node(_from(_D), _from(_C)),
        _VECTOR,
        expected_content_hash="h",
        expected_provenance=(_from(_D),),
    )

    assert engine.holds() == [f"hold {_C}"]


def test_entries_naming_no_conversation_hold_nothing() -> None:
    """A run's id, an entry with no interaction, and a legacy entry with no kind are not held."""
    engine = _Engine(gone=frozenset({_C}))

    _backend(engine).insert_node(
        "u1",
        _node(_from("run-1", kind="agentic_run"), _from(None), _from(_C, kind=None)),
        _VECTOR,
    )
    _backend(engine).upsert_edge("u1", _edge(None))

    assert engine.holds() == []


def test_the_guard_off_holds_nothing() -> None:
    engine = _Engine(gone=frozenset({_C}))

    _backend(engine, guard=False).insert_node("u1", _node(_from(_C)), _VECTOR)

    assert engine.holds() == []
    assert "insert" in engine.entries()


def test_each_cited_conversation_is_held_once_in_a_stable_order() -> None:
    engine = _Engine()

    _backend(engine).insert_node("u1", _node(_from(_D), _from(_C), _from(_D)), _VECTOR)

    assert engine.holds() == [f"hold {_C}", f"hold {_D}"]


class _FkEngine(_Engine):
    """Every INSERT fails as Postgres does when a row it references was deleted meanwhile."""

    def _open(self) -> _Conn:
        self._next += 1
        engine = self

        class _FkConn(_Conn):
            def execute(self, stmt: Any, params: dict[str, str] | None = None) -> _Result:  # noqa: ANN401
                if str(stmt).lstrip().upper().startswith("INSERT"):
                    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

                    class _Orig:
                        sqlstate = engine.sqlstate

                    raise IntegrityError("INSERT", {}, _Orig())  # type: ignore[arg-type]
                return super().execute(stmt, params)

        return _FkConn(self, self._next)

    sqlstate = "23503"


@pytest.mark.parametrize(
    "write",
    [
        lambda b: b.upsert_edge("u1", _edge(None)),
        lambda b: b.associate_entities("u1", "n1", ["x"]),
    ],
    ids=["semantic_link", "entity_link"],
)
def test_a_link_to_a_node_removed_meanwhile_is_a_link_target_gone_error(write: Any) -> None:  # noqa: ANN401
    """A follow-on write after the forget removed the node (R9-304 review, item 3)."""
    from persona.graph.errors import GraphLinkTargetGoneError  # noqa: PLC0415

    with pytest.raises(GraphLinkTargetGoneError):
        write(_backend(_FkEngine()))


def test_another_integrity_error_is_not_relabelled() -> None:
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415

    engine = _FkEngine()
    engine.sqlstate = "23505"

    with pytest.raises(IntegrityError):
        _backend(engine).upsert_edge("u1", _edge(None))


def test_append_provenance_writes_the_trail_only_under_the_node_cas() -> None:
    """A restatement's write: no embedding, no model, no content (R9-304 review round 2)."""
    from sqlalchemy.dialects import postgresql  # noqa: PLC0415

    captured: list[Any] = []

    class _Capture(_Conn):
        def execute(self, stmt: Any, params: dict[str, str] | None = None) -> _Result:  # noqa: ANN401
            if str(stmt).startswith("UPDATE graph_nodes"):
                captured.append(stmt.compile(dialect=postgresql.dialect()))
            return super().execute(stmt, params)

    class _CaptureEngine(_Engine):
        def _open(self) -> _Conn:
            self._next += 1
            return _Capture(self, self._next)

    assert _backend(_CaptureEngine()).append_provenance(
        "u1", "n1", _from(_C), expected_content_hash="h", expected_provenance=(_from(_D),)
    )

    (update,) = captured
    assert "updated_at" in update.params
    assert not {"embedding", "embedding_model", "content"} & set(update.params)
    sql = str(update)
    assert "SET provenance=(graph_nodes.provenance || jsonb_build_array(" in sql, "appended in SQL"
    assert "graph_nodes.content_hash = %(content_hash_1)s" in sql, "the hash the merge read"
    assert "jsonb_strip_nulls(graph_nodes.provenance) = jsonb_strip_nulls(" in sql, "and trail"
