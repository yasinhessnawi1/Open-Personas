"""R9-253: the fact store's error handling, without a database.

A fact write that loses the race with a persona erasure (the persona row goes between the
persona read and the foreign key check) is a refusal, not a crash; every other database
error propagates so the writer fails closed. The SQL is proven on Postgres in
``tests/integration/test_wellbeing_disclosure_store.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from persona_api.initiative import disclosures
from persona_api.initiative.disclosures import PgDisclosureStore
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, OperationalError

_PG = postgresql.dialect()

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Conn:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def execute(self, _stmt: Any) -> Any:  # noqa: ANN401
        raise self._error


def _failing(monkeypatch: pytest.MonkeyPatch, error: Exception) -> list[str]:
    owners: list[str] = []

    @contextmanager
    def connection(_engine: Any, owner_id: str) -> Iterator[_Conn]:  # noqa: ANN401
        owners.append(owner_id)
        yield _Conn(error)

    monkeypatch.setattr(disclosures, "rls_connection", connection)
    return owners


def test_losing_the_race_with_an_erasure_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    owners = _failing(monkeypatch, IntegrityError("INSERT", {}, Exception("fk")))
    assert PgDisclosureStore(object()).record("u", "p", "conv") is False  # type: ignore[arg-type]
    assert owners == ["u"], "written in the owner's own scope"


def test_any_other_database_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    _failing(monkeypatch, OperationalError("INSERT", {}, Exception("down")))
    with pytest.raises(OperationalError):
        PgDisclosureStore(object()).record("u", "p", "conv")  # type: ignore[arg-type]


def test_reading_no_refs_touches_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    owners = _failing(monkeypatch, OperationalError("SELECT", {}, Exception("down")))
    assert PgDisclosureStore(object()).disclosed("u", []) == set()  # type: ignore[arg-type]
    assert owners == []


class _Result:
    """A result whose ``rowcount`` must never decide anything (psycopg reports -1 for an
    ``INSERT ... SELECT ... ON CONFLICT DO NOTHING``, and -1 is truthy)."""

    def __init__(self, row: object | None) -> None:
        self._row = row

    @property
    def rowcount(self) -> int:
        raise AssertionError("record() must not trust rowcount")

    def first(self) -> object | None:
        return self._row


class _ScriptedConn:
    """Answers the persona read from ``persona_row``; records every statement it is given."""

    def __init__(self, persona_row: object | None) -> None:
        self._persona_row = persona_row
        self.statements: list[str] = []

    def execute(self, stmt: Any) -> _Result:  # noqa: ANN401
        sql = str(stmt.compile(dialect=_PG)).upper()
        self.statements.append(sql)
        if sql.startswith("SELECT"):
            return _Result(self._persona_row)
        return _Result(None)


def _scripted(monkeypatch: pytest.MonkeyPatch, persona_row: object | None) -> _ScriptedConn:
    conn = _ScriptedConn(persona_row)

    @contextmanager
    def connection(_engine: Any, _owner_id: str) -> Iterator[_ScriptedConn]:  # noqa: ANN401
        yield conn

    monkeypatch.setattr(disclosures, "rls_connection", connection)
    return conn


def test_a_persona_that_is_gone_is_refused_and_nothing_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI failure on PR #40: an erased persona's fact was reported as held."""
    conn = _scripted(monkeypatch, persona_row=None)
    assert PgDisclosureStore(object()).record("u", "pers_erased", "conv") is False  # type: ignore[arg-type]
    assert len(conn.statements) == 1, "only the persona read; no insert for a gone persona"
    assert "FOR KEY SHARE" in conn.statements[0], "the persona row is held until commit"


def test_a_live_persona_holds_the_fact_whatever_rowcount_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = _scripted(monkeypatch, persona_row=("pers",))
    assert PgDisclosureStore(object()).record("u", "pers", "conv") is True  # type: ignore[arg-type]
    assert conn.statements[0].startswith("SELECT")
    assert conn.statements[1].startswith("INSERT INTO WELLBEING_DISCLOSURES")
    assert "ON CONFLICT DO NOTHING" in conn.statements[1]
