"""Every graph write that names a persona holds its row first (R9-284, the write guard).

With the guard on, the backend runs ``SELECT 1 FROM personas ... FOR KEY SHARE`` for each
persona a write names, in the write's own transaction and before the write; a persona with no
row refuses the write. Proved on Postgres in packages/api's
test_graph_persona_guard_postgres.py; this pins which writes take it, and in what order, with a
recording connection.
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


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def first(self) -> object:
        return self._row

    def scalar_one(self) -> int:
        return 1

    def scalar_one_or_none(self) -> int:
        return 1


class _Conn:
    def __init__(self, gone: set[str]) -> None:
        self.gone = gone
        self.log: list[str] = []

    def execute(self, stmt: Any, params: dict[str, str] | None = None) -> _Result:  # noqa: ANN401
        sql = str(stmt)
        if "FOR KEY SHARE" in sql:
            assert params is not None
            self.log.append(f"hold {params['p']}")
            return _Result(None if params["p"] in self.gone else (1,))
        self.log.append(sql.split()[0].lower())
        return _Result((1,))


class _Engine:
    def __init__(self, gone: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.conn = _Conn(set(gone))

    @contextmanager
    def begin(self) -> Iterator[_Conn]:
        yield self.conn


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


def _backend(engine: _Engine, *, guard: bool = True) -> PostgresGraphBackend:
    return PostgresGraphBackend(engine=engine, persona_guard=guard)  # type: ignore[arg-type]


_VECTOR = [0.0] * 384


@pytest.mark.parametrize(
    "write",
    [
        lambda b: b.insert_node("u1", _node(_P, None), _VECTOR),
        lambda b: b.insert_node_if_absent("u1", _node(_P), _VECTOR),
        lambda b: b.update_node("u1", _node(None, _P), _VECTOR),
        lambda b: b.upsert_edge(
            "u1",
            TypedLink(
                id="e1",
                src_node_id="a",
                dst_node_id="b",
                link_type=LinkType.CAUSAL,
                provenance=_by(_P),
                created_at=_NOW,
            ),
        ),
        lambda b: b.insert_entity(
            "u1",
            CanonicalEntity(id="x", canonical_name="x", provenance=_by(_P), created_at=_NOW),
            _VECTOR,
        ),
    ],
    ids=["insert_node", "insert_node_if_absent", "update_node", "upsert_edge", "insert_entity"],
)
def test_a_write_holds_the_persona_it_names_before_writing(write: Any) -> None:  # noqa: ANN401
    engine = _Engine()

    write(_backend(engine))

    assert engine.conn.log[0] == f"hold {_P}"
    assert len(engine.conn.log) == 2, engine.conn.log


@pytest.mark.parametrize(
    "write",
    [
        lambda b: b.insert_node("u1", _node(_P), _VECTOR),
        lambda b: b.update_node("u1", _node(None), _VECTOR, guard_personas=[_P]),
        lambda b: b.upsert_edge(
            "u1",
            TypedLink(
                id="e1",
                src_node_id="a",
                dst_node_id="b",
                link_type=LinkType.CAUSAL,
                provenance=_by(None),
                created_at=_NOW,
            ),
            guard_personas=[_P],
        ),
    ],
    ids=["named_in_provenance", "update_for_a_member", "mirror_for_a_member"],
)
def test_a_write_naming_an_erased_persona_writes_nothing(write: Any) -> None:  # noqa: ANN401
    engine = _Engine(gone={_P})

    with pytest.raises(GraphPersonaGoneError):
        write(_backend(engine))

    assert engine.conn.log == [f"hold {_P}"], "nothing is written after the refusal"


def test_every_persona_a_trail_names_is_held_once() -> None:
    engine = _Engine()

    _backend(engine).update_node("u1", _node(_Q, _P, _Q, None), _VECTOR)

    assert engine.conn.log[:2] == [f"hold {_P}", f"hold {_Q}"]


def test_a_write_with_no_persona_and_the_guard_off_hold_nothing() -> None:
    engine = _Engine(gone={_P})

    _backend(engine).insert_node("u1", _node(None), _VECTOR)
    _backend(engine, guard=False).insert_node("u1", _node(_P), _VECTOR)

    assert [e for e in engine.conn.log if e.startswith("hold")] == []
