"""A node delete releases what consolidation copied from it, under the graph lock (R9-287).

The statement order on a fake Postgres connection: the lock timeout, the owner's graph
maintenance lock, the releaser's work, the DELETE, then the releaser's after-step; without a
releaser, the plain delete. A lock timeout becomes GraphNodeBusyError.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.errors import GraphNodeBusyError
from persona.graph.postgres import DELETE_LOCK_TIMEOUT, PostgresGraphBackend
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Result:
    def scalar_one_or_none(self) -> int:
        return 7


class _Conn:
    def __init__(self, log: list[str], *, busy: bool = False) -> None:
        self.dialect = postgresql.dialect()
        self.log = log
        self.busy = busy

    def execute(self, stmt: Any, params: Any = None) -> _Result:  # noqa: ANN401, ARG002
        sql = " ".join(str(stmt).split())
        if "pg_advisory_xact_lock" in sql:
            self.log.append("graph lock")
            if self.busy:
                orig = type("Orig", (Exception,), {"sqlstate": "55P03"})()
                raise OperationalError(sql, {}, orig)
        elif sql.startswith("SET LOCAL lock_timeout"):
            self.log.append(sql)
        elif sql.startswith("DELETE FROM graph_nodes"):
            self.log.append("delete")
        return _Result()


class _Engine:
    def __init__(self, log: list[str], *, busy: bool = False) -> None:
        self.log = log
        self.busy = busy

    @contextmanager
    def begin(self) -> Iterator[_Conn]:
        yield _Conn(self.log, busy=self.busy)


class _Releaser:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def release(self, conn: Any, owner_id: str, node_id: str) -> Any:  # noqa: ANN401, ARG002
        self.log.append(f"release {node_id}")
        return lambda: self.log.append("after")


def _backend(log: list[str], *, releaser: bool, busy: bool = False) -> PostgresGraphBackend:
    return PostgresGraphBackend(
        engine=_Engine(log, busy=busy),  # type: ignore[arg-type]
        persona_guard=False,
        node_releaser=_Releaser(log) if releaser else None,
    )


def test_a_delete_with_a_releaser_takes_the_lock_releases_deletes_then_finishes() -> None:
    log: list[str] = []

    assert _backend(log, releaser=True).delete_node("u1", "n1") == 7

    assert log == [
        f"SET LOCAL lock_timeout = '{DELETE_LOCK_TIMEOUT}'",
        "graph lock",
        "release n1",
        "delete",
        "after",
    ]


def test_the_lock_waits_at_most_fifteen_seconds() -> None:
    assert DELETE_LOCK_TIMEOUT == "15s"


def test_without_a_releaser_it_is_the_plain_delete() -> None:
    log: list[str] = []

    _backend(log, releaser=False).delete_node("u1", "n1")

    assert log == ["delete"]


def test_a_lock_timeout_is_a_busy_node_and_nothing_is_deleted() -> None:
    log: list[str] = []

    with pytest.raises(GraphNodeBusyError):
        _backend(log, releaser=True, busy=True).delete_node("u1", "n1")

    assert "delete" not in log
    assert "release n1" not in log
