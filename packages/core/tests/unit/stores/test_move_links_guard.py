"""The Postgres link move is guarded twice (R9-271, PR #39 review round 1).

``_move_links`` locks the superseded versions and compares them with what the writer read. The
UPDATE that moves each link then names the same version and requires the row still to be
current, and counts what it changed, so the move stays right even if the lock above it were ever
lost. These run against a recording connection: what the statement says is the point, and real
Postgres is in the api integration suite.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.errors import ChainConflictError
from persona.stores import postgres as pg
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Select, Update


class _Result:
    def __init__(self, rows: list[tuple[str, int, str | None]], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def all(self) -> list[tuple[str, int, str | None]]:
        return self._rows


class _Recording:
    """Answers the lock with ``found`` and every UPDATE with ``updated`` rows changed."""

    def __init__(self, found: list[tuple[str, int, str | None]], updated: int) -> None:
        self._found = found
        self._updated = updated
        self.updates: list[str] = []

    def execute(self, statement: Any, *_: object) -> _Result:  # noqa: ANN401 - any statement
        if isinstance(statement, Select):
            return _Result(self._found, len(self._found))
        assert isinstance(statement, Update)
        compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
        self.updates.append(str(compiled))
        return _Result([], self._updated)


def test_a_link_that_moved_nothing_is_a_conflict() -> None:
    conn = _Recording(found=[("f1", 1, None)], updated=0)
    with pytest.raises(ChainConflictError):
        pg._move_links(conn, "p1", "self_facts", {"f1": (1, "f2")})  # type: ignore[arg-type]


def test_a_link_that_moved_its_row_passes() -> None:
    conn = _Recording(found=[("f1", 1, None)], updated=1)
    pg._move_links(conn, "p1", "self_facts", {"f1": (1, "f2")})  # type: ignore[arg-type]
    assert len(conn.updates) == 1


def test_the_update_names_the_version_read_and_requires_the_row_current() -> None:
    conn = _Recording(found=[("f1", 1, None)], updated=1)
    pg._move_links(conn, "p1", "self_facts", {"f1": (1, "f2")})  # type: ignore[arg-type]
    (update,) = conn.updates
    assert "memory_chunks.version = " in update
    assert "memory_chunks.superseded_by IS NULL" in update
