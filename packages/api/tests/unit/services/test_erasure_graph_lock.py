"""The erasure's graph step takes the owner's graph maintenance lock first (D-R9-284-graph-mutex).

The sweep's backstop calls the same function, so this covers both. On Postgres the step then
waits for a running consolidation (packages/api test_graph_maintenance_lock_postgres.py).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.maintenance_lock import GRAPH_MAINTENANCE_CLASS
from persona_api.services.erasure_graph import erase_persona_graph

if TYPE_CHECKING:
    from collections.abc import Iterator

_DIALECT = type("D", (), {"name": "postgresql"})()


class _StopError(Exception):
    """Ends the step after its first statement: only the order is under test."""


class _Conn:
    dialect = _DIALECT

    def __init__(self, log: list[tuple[str, Any]]) -> None:
        self._log = log

    def execute(self, stmt: Any, params: Any = None) -> None:  # noqa: ANN401
        self._log.append((str(stmt), params))
        raise _StopError


class _Engine:
    dialect = _DIALECT

    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []

    @contextmanager
    def begin(self) -> Iterator[_Conn]:
        yield _Conn(self.log)


def test_the_graph_step_takes_the_owners_lock_before_any_row_lock() -> None:
    engine = _Engine()

    with pytest.raises(_StopError):
        erase_persona_graph(engine, owner_id="u1", persona_id="persona_" + "a" * 32)  # type: ignore[arg-type]

    ((first, params),) = engine.log
    assert first.startswith("SELECT pg_advisory_xact_lock(")
    assert f"{GRAPH_MAINTENANCE_CLASS}, hashtext('graph-maint:' || :owner)" in first
    assert params == {"owner": "u1"}


class _BusyResult:
    """Answers every query the step makes: the lock is taken by someone else, rows are none."""

    rowcount = 0

    def scalar(self) -> bool:
        return False

    def all(self) -> list[object]:
        return []

    def scalars(self) -> list[object]:
        return []

    def first(self) -> None:
        return None


class _BusyConn(_Conn):
    def execute(self, stmt: Any, params: Any = None) -> _BusyResult:  # noqa: ANN401
        self._log.append((str(stmt), params))
        return _BusyResult()


class _BusyEngine(_Engine):
    @contextmanager
    def begin(self) -> Iterator[_Conn]:
        yield _BusyConn(self.log)


def test_without_waiting_a_busy_lock_raises_before_any_other_statement() -> None:
    """Round 6 M-3: the sweep's call tries the lock once and gives up."""
    from persona_api.services.erasure_graph import GraphMaintenanceBusyError  # noqa: PLC0415

    engine = _BusyEngine()

    with pytest.raises(GraphMaintenanceBusyError):
        erase_persona_graph(engine, owner_id="u1", persona_id="persona_" + "a" * 32, wait=False)  # type: ignore[arg-type]

    ((first, params),) = engine.log
    assert first.startswith("SELECT pg_try_advisory_xact_lock(")
    assert params == {"owner": "u1"}


class _EmptyResult:
    rowcount = 0

    def all(self) -> list[object]:
        return []

    def scalars(self) -> list[object]:
        return []

    def first(self) -> None:
        return None

    def scalar(self) -> bool:
        return True


class _EmptyConn(_Conn):
    def execute(self, stmt: Any, params: Any = None) -> _EmptyResult:  # noqa: ANN401
        self._log.append((str(stmt), params))
        return _EmptyResult()


class _EmptyEngine(_Engine):
    @contextmanager
    def begin(self) -> Iterator[_Conn]:
        yield _EmptyConn(self.log)


def test_the_graph_step_ends_with_the_orphan_prune() -> None:
    """Round 6 M-1: the prune's counts are in the step's result, so the prune ran."""
    counts = erase_persona_graph(
        _EmptyEngine(),  # type: ignore[arg-type]
        owner_id="u1",
        persona_id="persona_" + "a" * 32,
    )

    assert counts["orphan_mirrors_deleted"] == 0
    assert counts["merge_records_pruned"] == 0


def test_the_erasures_unmerge_of_members_never_makes_a_second_open_row() -> None:
    """Round 8 MEDIUM-B: the same guarded reopen as consolidation's unmerge."""
    import json  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    from persona_api.services import erasure_graph  # noqa: PLC0415

    record = {"run_id": "r1", "canonical": "k", "mirrored_edges": [], "added_entities": []}
    member = SimpleNamespace(
        id="m", concept_name="m", content="c", metadata={"_k7_merge": json.dumps(record)}
    )
    log: list[tuple[str, Any]] = []

    class _Members(_EmptyResult):
        def all(self) -> list[object]:
            return [member]

    class _MemberConn(_Conn):
        def execute(self, stmt: Any, params: Any = None) -> _EmptyResult:  # noqa: ANN401
            log.append((str(stmt), params))
            return _Members() if len(log) == 1 else _EmptyResult()

    assert erasure_graph._unmerge_members_of(_MemberConn(log), "u1", ["k"]) == 1  # noqa: SLF001

    reopen = next(sql for sql, _ in log if sql.startswith("UPDATE graph_edges"))
    assert "NOT (EXISTS (SELECT open_twin.id" in reopen


def test_a_bounded_wait_sets_a_local_lock_timeout_before_the_lock() -> None:
    """Reviewer's M15 and M16: set_config(..., true) is SET LOCAL, and it comes first."""

    class _TwoStatements(_Conn):
        def execute(self, stmt: Any, params: Any = None) -> _EmptyResult:  # noqa: ANN401
            self._log.append((str(stmt), params))
            if len(self._log) == 2:
                raise _StopError
            return _EmptyResult()

    class _Engine2(_Engine):
        @contextmanager
        def begin(self) -> Iterator[_Conn]:
            yield _TwoStatements(self.log)

    engine = _Engine2()
    with pytest.raises(_StopError):
        erase_persona_graph(
            engine,  # type: ignore[arg-type]
            owner_id="u1",
            persona_id="persona_" + "a" * 32,
            lock_timeout_ms=60_000,
        )

    (timeout_sql, timeout_params), (lock_sql, _) = engine.log
    assert timeout_sql == "SELECT set_config('lock_timeout', :t, true)", "true: this txn only"
    assert timeout_params == {"t": "60000ms"}
    assert lock_sql.startswith("SELECT pg_advisory_xact_lock(")


def test_the_erasure_hashes_the_rows_own_content_not_a_stale_copy() -> None:
    """D-R9-284-node-cas: _set_metadata re-reads the name and content under a row lock."""
    from types import SimpleNamespace  # noqa: PLC0415

    from persona.graph.models import _compute_node_hash  # noqa: PLC0415
    from persona_api.services import erasure_graph  # noqa: PLC0415
    from sqlalchemy.dialects import postgresql  # noqa: PLC0415

    log: list[str] = []
    written: dict[str, Any] = {}

    class _Own(_EmptyResult):
        def first(self) -> object:
            return SimpleNamespace(concept_name="k", content="the content now")

    class _OwnConn(_Conn):
        def execute(self, stmt: Any, _params: Any = None) -> _EmptyResult:  # noqa: ANN401
            sql = str(stmt.compile(dialect=postgresql.dialect()))
            log.append(sql)
            if sql.startswith("UPDATE"):
                written.update(stmt.compile(dialect=postgresql.dialect()).params)
            return _Own()

    stale = SimpleNamespace(id="k", concept_name="k", content="the content then")
    erasure_graph._set_metadata(_OwnConn([]), "u1", stale, {"x": "1"})  # noqa: SLF001

    assert "FOR NO KEY UPDATE" in log[0], "the row is locked while it is hashed"
    assert written["content_hash"] == _compute_node_hash("k", "the content now", {"x": "1"})
