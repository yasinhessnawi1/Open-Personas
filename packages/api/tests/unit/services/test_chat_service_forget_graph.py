"""Forgetting a conversation's memory forgets its graph facts in the same commit (R9-304).

``chat_service.delete_conversation`` takes a ``graph_forget`` step and runs it inside the
transaction that deletes the conversation's row, before the delete. So the two are one unit:
if the graph step fails, the row stays and the delete can be retried, and a synthesis writing a
fact from this conversation either commits before the step (and is removed by it) or finds the
row gone (refused by the graph's guard). A plain delete never runs the step.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.errors import GraphNodeBusyError
from persona_api.errors import ConversationNotFoundError
from persona_api.services import chat_service
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def first(self) -> object:
        return self._row


class _Conn:
    def __init__(self, engine: _Engine) -> None:
        self._engine = engine

    def execute(self, stmt: Any, params: object = None) -> _Result:  # noqa: ANN401, ARG002
        sql = str(stmt)
        self._engine.log.append(("execute", sql.split()[0], id(self)))
        return _Result(("c1",) if self._engine.row_there else None)


class _Engine:
    def __init__(self, *, row_there: bool = True) -> None:
        self.row_there = row_there
        self.log: list[tuple[str, str, int]] = []
        self.committed = 0
        self.rolled_back = 0

    @contextmanager
    def begin(self) -> Iterator[_Conn]:
        conn = _Conn(self)
        try:
            yield conn
        except BaseException:
            self.rolled_back += 1
            raise
        self.committed += 1


@pytest.fixture(autouse=True)
def _no_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_service, "_conversation_message_texts", lambda *_a: [])


def _delete(engine: _Engine, graph_forget: Any, *, forget: bool = True) -> dict[str, int]:  # noqa: ANN401
    return chat_service.delete_conversation(
        rls_engine=engine,  # type: ignore[arg-type]
        conversation_id="c1",
        forget_memory=forget,
        persona_id="p1",
        graph_forget=graph_forget,
    )


def test_the_graph_step_runs_in_the_row_deletes_transaction_before_the_delete() -> None:
    engine = _Engine()
    seen: list[int] = []

    def _forget(conn: _Conn) -> dict[str, int]:
        seen.append(id(conn))
        engine.log.append(("graph", "forget", id(conn)))
        return {"nodes_deleted": 3}

    counts = _delete(engine, _forget)

    assert counts == {"nodes_deleted": 3}
    assert [entry[:2] for entry in engine.log] == [("graph", "forget"), ("execute", "DELETE")]
    assert {entry[2] for entry in engine.log} == set(seen), "one connection: one transaction"
    assert engine.committed == 1


def test_a_failed_graph_step_leaves_the_conversation_for_a_retry() -> None:
    engine = _Engine()

    def _forget(_conn: _Conn) -> dict[str, int]:
        msg = "the graph step failed"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError):
        _delete(engine, _forget)

    assert [entry[1] for entry in engine.log] == [], "the row delete never ran"
    assert engine.rolled_back == 1


def test_a_busy_graph_is_a_busy_error_and_nothing_is_deleted() -> None:
    engine = _Engine()

    class _Orig:
        sqlstate = "55P03"

    def _forget(_conn: _Conn) -> dict[str, int]:
        raise OperationalError("SELECT pg_advisory_xact_lock", {}, _Orig())  # type: ignore[arg-type]

    with pytest.raises(GraphNodeBusyError):
        _delete(engine, _forget)

    assert engine.rolled_back == 1


def test_another_database_error_is_not_called_busy() -> None:
    engine = _Engine()

    class _Orig:
        sqlstate = "08006"

    def _forget(_conn: _Conn) -> dict[str, int]:
        raise OperationalError("SELECT 1", {}, _Orig())  # type: ignore[arg-type]

    with pytest.raises(OperationalError):
        _delete(engine, _forget)


def test_a_plain_delete_never_runs_the_graph_step() -> None:
    engine = _Engine()
    ran: list[object] = []

    assert _delete(engine, ran.append, forget=False) == {}

    assert ran == []
    assert [entry[1] for entry in engine.log] == ["DELETE"]


def test_a_conversation_that_is_not_there_is_not_found_and_rolls_the_graph_back() -> None:
    engine = _Engine(row_there=False)

    with pytest.raises(ConversationNotFoundError):
        _delete(engine, lambda _conn: {"nodes_deleted": 1})

    assert engine.rolled_back == 1
