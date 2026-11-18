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
from sqlalchemy.exc import IntegrityError, OperationalError

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
