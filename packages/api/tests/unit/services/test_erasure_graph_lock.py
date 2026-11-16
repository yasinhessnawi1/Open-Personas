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


class _BusyConn(_Conn):
    def execute(self, stmt: Any, params: Any = None) -> Any:  # noqa: ANN401
        self._log.append((str(stmt), params))
        return type("R", (), {"scalar": lambda _self: False})()


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
