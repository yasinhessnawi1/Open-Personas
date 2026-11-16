"""Consolidation holds the owner's graph maintenance lock for its whole run (D-R9-284-graph-mutex).

Fakes only: the lock is taken before the run's first read, released when the run ends or fails,
and taken on its own session connection that is committed while held and never pooled if the
release fails. Proved on Postgres (an erasure's graph step waits for it) in packages/api's
test_graph_maintenance_lock_postgres.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import MemoryAuditLogger
from persona.graph.config import GraphSettings
from persona.graph.consolidation import ConsolidationPass
from persona.graph.maintenance_lock import (
    GRAPH_MAINTENANCE_CLASS,
    graph_maintenance_held,
    take_graph_maintenance_lock,
)

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Backend:
    """Records the lock and every read; ``fail`` names a read that raises."""

    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[str] = []
        self._fail = fail

    @contextmanager
    def maintenance_lock(self, owner_id: str) -> Iterator[None]:
        self.calls.append(f"lock {owner_id}")
        try:
            yield
        finally:
            self.calls.append(f"unlock {owner_id}")

    def _read(self, name: str) -> None:
        self.calls.append(name)
        if name == self._fail:
            raise RuntimeError(name)

    def pending_merges(self, _owner: str) -> list[str]:
        self._read("pending_merges")
        return []

    def get_marker(self, _owner: str) -> tuple[int, None]:
        self._read("get_marker")
        return 0, None

    def dirty_candidates(self, _owner: str, _watermark: object) -> list[object]:
        self._read("dirty_candidates")
        return []

    def apply_disuse_decay(self, *_a: Any, **_k: Any) -> None:  # noqa: ANN401
        self._read("apply_disuse_decay")

    def advance_marker(self, *_a: Any, **_k: Any) -> None:  # noqa: ANN401
        self._read("advance_marker")

    def get_node(self, _owner: str, _node_id: str) -> None:
        self._read("get_node")


def _pass(backend: _Backend) -> ConsolidationPass:
    return ConsolidationPass(
        backend=backend,  # type: ignore[arg-type]
        index=object(),  # type: ignore[arg-type]
        embedder=object(),  # type: ignore[arg-type]
        audit_logger=MemoryAuditLogger(),
        settings=GraphSettings(),
    )


def test_the_run_takes_the_lock_before_its_first_read_and_releases_it() -> None:
    backend = _Backend()

    _pass(backend).run("u1")

    assert backend.calls[0] == "lock u1", "nothing is read before the lock is held"
    assert backend.calls[1:3] == ["pending_merges", "get_marker"]
    assert backend.calls[-1] == "unlock u1"


@pytest.mark.parametrize(
    "fail", ["pending_merges", "get_marker", "dirty_candidates", "advance_marker"]
)
def test_a_failing_run_releases_the_lock(fail: str) -> None:
    backend = _Backend(fail=fail)

    with pytest.raises(RuntimeError, match=fail):
        _pass(backend).run("u1")

    assert backend.calls[0] == "lock u1"
    assert backend.calls[-1] == "unlock u1"


def test_unmerge_holds_the_lock_too() -> None:
    backend = _Backend()

    assert _pass(backend).unmerge("u1", "n1") is False

    assert backend.calls == ["lock u1", "get_node", "unlock u1"]


# --- the lock itself, on a fake engine --------------------------------------------------


class _Conn:
    def __init__(self, log: list[str], *, fail_unlock: bool) -> None:
        self._log = log
        self._fail_unlock = fail_unlock
        self.dialect = type("D", (), {"name": "postgresql"})()

    def execute(self, stmt: Any, params: dict[str, str]) -> None:  # noqa: ANN401
        sql = str(stmt)
        verb = "unlock" if "unlock" in sql else ("xact" if "xact" in sql else "lock")
        assert f"{GRAPH_MAINTENANCE_CLASS}, hashtext('graph-maint:' || :owner)" in sql
        self._log.append(f"{verb} {params['owner']}")
        if verb == "unlock" and self._fail_unlock:
            raise RuntimeError("the connection broke")

    def commit(self) -> None:
        self._log.append("commit")

    def invalidate(self) -> None:
        self._log.append("invalidate")

    def close(self) -> None:
        self._log.append("close")


class _Engine:
    def __init__(self, *, fail_unlock: bool = False) -> None:
        self.log: list[str] = []
        self._fail_unlock = fail_unlock
        self.dialect = type("D", (), {"name": "postgresql"})()

    def connect(self) -> _Conn:
        return _Conn(self.log, fail_unlock=self._fail_unlock)


def test_the_session_lock_is_committed_while_held_and_released_on_error() -> None:
    engine = _Engine()

    def _body() -> None:
        with graph_maintenance_held(engine, "u1"):  # type: ignore[arg-type]
            engine.log.append("body")
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        _body()

    assert engine.log == ["lock u1", "commit", "body", "unlock u1", "commit", "close"]


def test_a_failed_release_never_returns_the_session_to_the_pool() -> None:
    engine = _Engine(fail_unlock=True)

    with graph_maintenance_held(engine, "u1"):  # type: ignore[arg-type]
        pass

    assert engine.log == ["lock u1", "commit", "unlock u1", "invalidate", "close"]


def test_the_transaction_lock_is_one_statement_and_a_no_op_off_postgres() -> None:
    log: list[str] = []
    take_graph_maintenance_lock(_Conn(log, fail_unlock=False), "u1")  # type: ignore[arg-type]
    assert log == ["xact u1"]

    class _Sqlite:
        dialect = type("D", (), {"name": "sqlite"})()

        def execute(self, *_a: object) -> None:
            raise AssertionError("no lock off Postgres")

    take_graph_maintenance_lock(_Sqlite(), "u1")  # type: ignore[arg-type]
