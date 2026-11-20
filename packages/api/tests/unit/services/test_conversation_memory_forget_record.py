"""The in-transaction record step of a conversation forget: its locks and its capture (R9-311).

No database: statements are compiled for Postgres and read. The lock order is the erasures'
(the users row, then the persona, then the conversation), so a forget and a persona or account
erasure running together cannot wait on each other in a circle. The capture takes every
version of a stamped chain, so a retry still reaches what was built on an unstamped version.
The behaviour is proved on Postgres in
tests/integration/test_conversation_memory_forget_postgres.py.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona_api.services import conversation_memory_forget as service
from persona_api.services import forget_conversation_graph as fcg
from sqlalchemy.dialects import postgresql

_PG = postgresql.dialect()


def _sql(stmt: Any) -> str:  # noqa: ANN401
    if isinstance(stmt, str):
        return stmt
    return " ".join(str(stmt.compile(dialect=_PG)).split())


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value

    def scalars(self) -> list[object]:
        return list(self._value) if isinstance(self._value, list) else []

    def first(self) -> object:
        return self._value

    def mappings(self) -> _Result:
        return self


class _Dialect:
    name = "postgresql"


class _Conn:
    """Logs each statement compiled for Postgres; answers what the step reads."""

    dialect = _Dialect()

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.params: list[dict[str, Any]] = []

    def execute(self, stmt: Any, params: object = None) -> _Result:  # noqa: ANN401, ARG002
        sql = _sql(stmt)
        self.sql.append(sql)
        self.params.append({} if isinstance(stmt, str) else dict(stmt.compile(dialect=_PG).params))
        if "FROM memory_chunks" in sql:
            return _Result(["s1", "v2"])
        if sql.startswith("SELECT conversations.persona_id"):
            return _Result("p1")
        return _Result(None)


def _index(conn: _Conn, needle: str) -> int:
    return next(i for i, sql in enumerate(conn.sql) if needle in sql)


def _starting(conn: _Conn, prefix: str) -> int:
    return next(i for i, sql in enumerate(conn.sql) if sql.startswith(prefix))


def test_the_owner_rows_are_held_before_the_conversation_is_locked() -> None:
    conn = _Conn()

    captured = service.record(conn, [], owner_id="u1", conversation_id="c1", durable=False)  # type: ignore[arg-type]

    assert captured == ["s1", "v2"]
    users = _index(conn, "FROM users")
    personas = _index(conn, "FROM personas")
    conversation = _starting(conn, "SELECT conversations.persona_id")
    assert users < personas < conversation, "users, persona, conversation: the erasures' order"
    assert conn.sql[users].endswith("FOR KEY SHARE")
    assert conn.sql[personas].endswith("FOR KEY SHARE OF personas")
    assert conn.sql[conversation].endswith("FOR UPDATE")


def test_the_graph_step_holds_the_owner_rows_between_its_lock_and_the_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fcg, "take_source_out", lambda *_a: {})
    conn = _Conn()

    fcg.forget_conversation_graph(conn, owner_id="u1", conversation_id="c1")  # type: ignore[arg-type]

    lock = _index(conn, "pg_advisory_xact_lock")
    users = _starting(conn, "SELECT users.id")
    personas = _starting(conn, "SELECT personas.id")
    conversation = _starting(conn, "SELECT conversations.id")
    assert lock < users < personas < conversation, "nothing held before the graph lock"


def test_the_capture_takes_every_version_of_a_stamped_chain() -> None:
    """A rollback can write an unstamped version 2: it is captured with its stamped chain."""
    conn = _Conn()

    service.record(conn, [], owner_id="u1", conversation_id="c1", durable=False)  # type: ignore[arg-type]

    capture = conn.sql[_index(conn, "FROM memory_chunks")]
    assert "memory_chunks.logical_id IN (SELECT memory_chunks.logical_id" in capture
    assert capture.count("memory_chunks.metadata ->>") == 2, capture


def test_a_durable_record_enqueues_every_id_with_the_mark() -> None:
    conn = _Conn()

    service.record(conn, ["legacy1"], owner_id="u1", conversation_id="c1", durable=True)  # type: ignore[arg-type]

    mark = _index(conn, "INSERT INTO forgotten_conversations")
    job = _index(conn, "INSERT INTO jobs")
    assert mark < job


def test_the_job_carries_the_ids_captured_under_the_lock_as_well_as_those_found_before() -> None:
    """The pre-found legacy ids and the stamped ids read under the row lock, all of them."""
    conn = _Conn()

    service.record(conn, ["legacy1"], owner_id="u1", conversation_id="c1", durable=True)  # type: ignore[arg-type]

    job = conn.params[_index(conn, "INSERT INTO jobs")]
    assert job["type"] == service.JOB_TYPE
    assert job["owner_id"] == "u1"
    assert job["payload"] == {
        "persona_id": "p1",
        "conversation_id": "c1",
        "chunk_ids": ["legacy1", "s1", "v2"],
    }
    assert job["idempotency_key"] == "conversation_memory_forget:c1"


def test_the_mark_is_for_exactly_this_conversation_owner_and_persona() -> None:
    conn = _Conn()

    service.record(conn, [], owner_id="u1", conversation_id="c1", durable=False)  # type: ignore[arg-type]

    mark = conn.params[_index(conn, "INSERT INTO forgotten_conversations")]
    assert mark == {"conversation_id": "c1", "owner_id": "u1", "persona_id": "p1"}
