"""Every graph write that names a persona holds its row first, in the same transaction (R9-284).

With the guard on, the backend runs ``SELECT 1 FROM personas ... FOR KEY SHARE`` for each
persona a write names, in the write's own transaction and before the write; a persona with no
row refuses the write. The point of the ruling is the shared transaction: a hold committed
before the write would release the lock and let the erasure's delete through. So the fake
engine opens a numbered transaction for every ``begin()`` and ``connect()``, and the tests
assert the holds and the write ran in one, holds first. Proved on Postgres in packages/api's
test_graph_persona_guard_postgres.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.errors import GraphPersonaGoneError
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    EntityAlias,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
)
from persona.graph.postgres import PostgresGraphBackend
from persona.schema.chunks import WriteSource

if TYPE_CHECKING:
    from collections.abc import Iterator

_NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
_P = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_Q = "persona_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_VECTOR = [0.0] * 384


def _node_row() -> dict[str, Any]:
    """A ``graph_nodes`` row as snapshot_node_version reads it."""
    return {
        "node_kind": "fact",
        "concept_name": "n1",
        "content": "about n1",
        "metadata": {},
        "wellbeing_category": None,
        "embedding": _VECTOR,
        "embedding_model": "m",
        "content_hash": "h",
        "provenance": [{"source": "system", "persona_id": _P, "written_at": _NOW.isoformat()}],
    }


def _entity_row() -> dict[str, Any]:
    """A ``graph_entities`` row as get_entity reads it."""
    return {"id": "x", "canonical_name": "x", "aliases": [], "provenance": None, "created_at": _NOW}


class _Mappings:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def one_or_none(self) -> dict[str, Any] | None:
        return self._row


class _Result:
    def __init__(self, row: object = (1,), mapping: dict[str, Any] | None = None) -> None:
        self._row = row
        self._mapping = mapping

    def first(self) -> object:
        return self._row

    def scalar_one(self) -> int:
        return 1

    def scalar_one_or_none(self) -> int:
        return 1

    def scalars(self) -> list[str]:
        return ["x"]

    def mappings(self) -> _Mappings:
        return _Mappings(self._mapping)


class _Conn:
    """One transaction: every statement is logged with this transaction's number."""

    def __init__(self, engine: _Engine, txn: int) -> None:
        self._engine = engine
        self._txn = txn

    def execute(self, stmt: Any, params: dict[str, str] | None = None) -> _Result:  # noqa: ANN401
        sql = str(stmt)
        self._engine.statements.append((self._txn, sql))
        if "FOR KEY SHARE" in sql:
            assert params is not None
            self._engine.log.append((self._txn, f"hold {params['p']}"))
            return _Result(None if params["p"] in self._engine.gone else (1,))
        verb = sql.split()[0].lower()
        self._engine.log.append((self._txn, verb))
        if verb == "select" and "graph_entities" in sql:
            return _Result(mapping=_entity_row())
        if verb == "select":
            return _Result(mapping=_node_row())
        return _Result()


class _Engine:
    def __init__(self, gone: frozenset[str] = frozenset()) -> None:
        self.gone = set(gone)
        self.log: list[tuple[int, str]] = []
        self.statements: list[tuple[int, str]] = []
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

    def transaction_of(self, verb: str) -> int:
        (txn,) = {t for t, entry in self.log if entry == verb}
        return txn


def _by(persona_id: str | None) -> NodeProvenance:
    return NodeProvenance(source=WriteSource.SYSTEM, persona_id=persona_id, written_at=_NOW)


def _node(*by: str | None) -> ConceptNode:
    return ConceptNode(
        id="n1",
        node_kind=NodeKind.FACT,
        concept_name="n1",
        content="about n1",
        provenance=tuple(_by(p) for p in by),
        created_at=_NOW,
    )


def _edge(by: str | None) -> TypedLink:
    return TypedLink(
        id="e1",
        src_node_id="a",
        dst_node_id="b",
        link_type=LinkType.CAUSAL,
        provenance=_by(by),
        created_at=_NOW,
    )


def _backend(engine: _Engine, *, guard: bool = True) -> PostgresGraphBackend:
    return PostgresGraphBackend(engine=engine, persona_guard=guard)  # type: ignore[arg-type]


_WRITES = {
    "insert_node": (lambda b: b.insert_node("u1", _node(_P, None), _VECTOR), "insert"),
    "insert_node_if_absent": (
        lambda b: b.insert_node_if_absent("u1", _node(_P), _VECTOR),
        "insert",
    ),
    "update_node": (lambda b: b.update_node("u1", _node(None, _P), _VECTOR), "update"),
    "upsert_edge": (lambda b: b.upsert_edge("u1", _edge(_P)), "insert"),
    "insert_entity": (
        lambda b: b.insert_entity(
            "u1",
            CanonicalEntity(id="x", canonical_name="x", provenance=_by(_P), created_at=_NOW),
            _VECTOR,
        ),
        "insert",
    ),
    "snapshot_node_version": (
        lambda b: b.snapshot_node_version(
            "u1", "n1", valid_at=_NOW, invalid_at=_NOW, invalidated_by=None
        ),
        "insert",
    ),
    "add_alias": (
        lambda b: b.add_alias("u1", "x", EntityAlias(surface="my doctor", persona_id=_P)),
        "update",
    ),
    # Round 7: every other method that takes guard_personas.
    "associate_entities": (
        lambda b: b.associate_entities("u1", "n1", ["x"], guard_personas=[_P]),
        "insert",
    ),
    "associate_entities_returning": (
        lambda b: b.associate_entities_returning("u1", "n1", ["x"], guard_personas=[_P]),
        "insert",
    ),
    "associate_entities_recording": (
        lambda b: b.associate_entities_recording(
            "u1",
            "k",
            ["x"],
            member_id="m",
            merged_into="k",
            record=lambda added: ({"_k7_merge": str(added)}, "h"),
            guard_personas=[_P],
        ),
        "insert",
    ),
    "insert_edge_if_absent": (
        lambda b: b.insert_edge_if_absent("u1", _edge(None), guard_personas=[_P]),
        "insert",
    ),
    "set_merged_into": (
        lambda b: b.set_merged_into(
            "u1", "m", merged_into="k", metadata={}, content_hash="h", guard_personas=[_P]
        ),
        "update",
    ),
}


@pytest.mark.parametrize("name", list(_WRITES))
def test_the_hold_and_the_write_share_one_transaction_holds_first(name: str) -> None:
    write, verb = _WRITES[name]
    engine = _Engine()

    write(_backend(engine))

    txn = engine.transaction_of(verb)
    in_the_write = [entry for t, entry in engine.log if t == txn]
    assert f"hold {_P}" in in_the_write, f"{name}: the hold is not in the write's transaction"
    assert in_the_write.index(f"hold {_P}") < in_the_write.index(verb), f"{name}: write first"
    assert [entry for t, entry in engine.log if t != txn and entry.startswith("hold")] == []


@pytest.mark.parametrize(
    "write",
    [
        lambda b: b.insert_node("u1", _node(_P), _VECTOR),
        lambda b: b.update_node("u1", _node(None), _VECTOR, guard_personas=[_P]),
        lambda b: b.upsert_edge("u1", _edge(None), guard_personas=[_P]),
        lambda b: b.add_alias("u1", "x", EntityAlias(surface="my doctor", persona_id=_P)),
    ],
    ids=["named_in_provenance", "update_for_a_member", "mirror_for_a_member", "alias"],
)
def test_a_write_naming_an_erased_persona_writes_nothing(write: Any) -> None:  # noqa: ANN401
    engine = _Engine(gone=frozenset({_P}))

    with pytest.raises(GraphPersonaGoneError):
        write(_backend(engine))

    assert [e for e in engine.entries() if e in {"insert", "update"}] == [], "nothing written"


def test_every_persona_a_trail_names_is_held_once() -> None:
    engine = _Engine()

    _backend(engine).update_node("u1", _node(_Q, _P, _Q, None), _VECTOR)

    assert engine.entries()[:2] == [f"hold {_P}", f"hold {_Q}"]


def test_a_write_with_no_persona_and_the_guard_off_hold_nothing() -> None:
    engine = _Engine(gone=frozenset({_P}))

    _backend(engine).insert_node("u1", _node(None), _VECTOR)
    _backend(engine, guard=False).insert_node("u1", _node(_P), _VECTOR)

    assert [e for e in engine.entries() if e.startswith("hold")] == []


def test_add_alias_appends_in_one_statement_in_the_held_transaction() -> None:
    """Review MEDIUM-2: no read on another connection; the append and the dedupe are SQL."""
    engine = _Engine()

    _backend(engine).add_alias("u1", "x", EntityAlias(surface="my doctor", persona_id=_P))

    assert engine.entries() == [f"hold {_P}", "update"], "one transaction, no separate read"
    assert len({txn for txn, _ in engine.log}) == 1
    (append,) = [sql for _, sql in engine.statements if sql.startswith("UPDATE")]
    assert "graph_entities.aliases ||" in append, "appended in SQL, not rewritten from a read"
    assert "NOT (graph_entities.aliases @>" in append, "an alias already there is not repeated"
