"""A node delete releases what consolidation copied from it, under the graph lock (R9-287).

The statement order on a fake Postgres connection: the lock timeout, the owner's graph
maintenance lock, the releaser's work, the DELETE, then the releaser's after-step; without a
releaser, the plain delete. A lock timeout becomes GraphNodeBusyError.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.errors import GraphError, GraphNodeBusyError
from persona.graph.postgres import (
    DELETE_LOCK_TIMEOUT,
    NodeRelease,
    PostgresGraphBackend,
    RefuseNodeDeletes,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Result:
    def __init__(self, ids: list[str] | None = None) -> None:
        self.ids = ids or []

    def all(self) -> list[tuple[str, int]]:
        return [(node_id, 7 + i) for i, node_id in enumerate(self.ids)]


class _Conn:
    def __init__(self, log: list[str], *, busy: str | None = None) -> None:
        self.dialect = postgresql.dialect()
        self.log = log
        self.busy = busy

    def execute(self, stmt: Any, params: Any = None) -> _Result:  # noqa: ANN401, ARG002
        sql = " ".join(str(stmt).split())
        if "pg_advisory_xact_lock" in sql:
            self.log.append("graph lock")
            if self.busy is not None:
                orig = type("Orig", (Exception,), {"sqlstate": self.busy})()
                raise OperationalError(sql, {}, orig)
        elif sql.startswith("SET LOCAL lock_timeout"):
            self.log.append(sql)
        elif sql.startswith("DELETE FROM graph_nodes"):
            lists = [v for v in stmt.compile().params.values() if isinstance(v, list)]
            ids = [str(i) for i in (lists[0] if lists else [])]
            self.log.append("delete " + ",".join(ids) if len(ids) > 1 else "delete")
            return _Result(ids)
        return _Result()


class _Engine:
    def __init__(self, log: list[str], *, busy: str | None = None) -> None:
        self.log = log
        self.busy = busy

    @contextmanager
    def begin(self) -> Iterator[_Conn]:
        yield _Conn(self.log, busy=self.busy)


class _Releaser:
    def __init__(self, log: list[str], members: tuple[str, ...] = ()) -> None:
        self.log = log
        self.members = members

    def release(self, conn: Any, owner_id: str, node_id: str) -> NodeRelease:  # noqa: ANN401, ARG002
        self.log.append(f"release {node_id}")
        return NodeRelease(members=self.members, after=lambda: self.log.append("after"))


def _backend(
    log: list[str], *, releaser: bool, busy: str | None = None, timeout: str | None = None
) -> PostgresGraphBackend:
    extra = {} if timeout is None else {"delete_lock_timeout": timeout}
    return PostgresGraphBackend(
        engine=_Engine(log, busy=busy),  # type: ignore[arg-type]
        persona_guard=False,
        node_releaser=_Releaser(log) if releaser else None,
        **extra,
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


@pytest.mark.parametrize("sqlstate", ["55P03", "40P01"])
def test_a_lock_timeout_or_deadlock_is_a_busy_node_and_nothing_is_deleted(sqlstate: str) -> None:
    log: list[str] = []

    with pytest.raises(GraphNodeBusyError):
        _backend(log, releaser=True, busy=sqlstate).delete_node("u1", "n1")

    assert "delete" not in log
    assert "release n1" not in log


@pytest.mark.parametrize("sqlstate", ["57014", "08006", "40001"])
def test_any_other_operational_error_is_raised_as_it_is(sqlstate: str) -> None:
    """Review MEDIUM-2: only a lock timeout or a deadlock is a 409; a cancel, a lost connection
    or a serialization failure is not the graph being reorganised."""
    with pytest.raises(OperationalError):
        _backend([], releaser=True, busy=sqlstate).delete_node("u1", "n1")


def test_the_lock_timeout_can_be_set_and_is_used() -> None:
    log: list[str] = []

    _backend(log, releaser=True, timeout="200ms").delete_node("u1", "n1")

    assert log[0] == "SET LOCAL lock_timeout = '200ms'"


@pytest.mark.parametrize("bad", ["15", "1s'; DROP TABLE x; --", "0s", "15 s", ""])
def test_a_lock_timeout_of_any_other_shape_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="delete_lock_timeout"):
        _backend([], releaser=True, timeout=bad)


def test_a_process_that_refuses_node_deletes_deletes_nothing() -> None:
    log: list[str] = []
    backend = PostgresGraphBackend(
        engine=_Engine(log),  # type: ignore[arg-type]
        persona_guard=False,
        node_releaser=RefuseNodeDeletes(),
    )

    with pytest.raises(GraphError, match="does not delete graph nodes"):
        backend.delete_node("u1", "n1")

    assert "delete" not in log


def test_the_members_go_in_the_same_statement_and_the_node_keeps_its_surrogate() -> None:
    """D-R9-287-forget-canonical: what was merged into the node goes with it."""
    log: list[str] = []
    backend = PostgresGraphBackend(
        engine=_Engine(log),  # type: ignore[arg-type]
        persona_guard=False,
        node_releaser=_Releaser(log, members=("m1", "m2")),
    )

    assert backend.delete_node("u1", "n1") == 7

    assert log[-2:] == ["delete n1,m1,m2", "after"]


def test_the_backend_returns_every_node_it_deleted() -> None:
    """Round 3 LOW-4: the node and its members, each with its surrogate, from the RETURNING."""
    log: list[str] = []
    backend = PostgresGraphBackend(
        engine=_Engine(log),  # type: ignore[arg-type]
        persona_guard=False,
        node_releaser=_Releaser(log, members=("m1", "m2")),
    )

    assert backend.delete_node_and_members("u1", "n1") == {"n1": 7, "m1": 8, "m2": 9}
