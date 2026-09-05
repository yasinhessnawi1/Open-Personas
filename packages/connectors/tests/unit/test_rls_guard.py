"""The owner-scoped engine must run as a role Postgres subjects to RLS (R9-123).

The connectors process connected its RLS engine as the ``persona`` superuser. Every
``owner_scope`` was set correctly and honoured by nothing: superusers and BYPASSRLS
roles are exempt from every policy, FORCE or not. The Telegram roster listed all
fifteen personas across five accounts, and a turn addressed to another tenant's
persona was stopped only by a foreign key. These pin the guard that makes that
configuration fail instead of leak.

The role query needs Postgres; the live proof runs in the integration leg. Here the
DBAPI seam is faked and the wiring is checked structurally.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona_api.middleware.rls_context import make_rls_engine
from persona_connectors.composition import ConnectorComposition
from persona_connectors.config import ConnectorConfig
from persona_connectors.rls_guard import (
    guard_rls_engine_role,
    role_bypasses_rls,
    verify_rls_engine_role,
)
from sqlalchemy import create_engine


class _Cursor:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self._row = row
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def close(self) -> None:
        self.closed = True


class _Conn:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.cursor_obj = _Cursor(row)

    def cursor(self) -> _Cursor:
        return self.cursor_obj


@pytest.mark.parametrize(
    ("row", "expected"),
    [((True,), True), ((False,), False), (None, False)],
    ids=["superuser-or-bypassrls", "ordinary-role", "role-missing"],
)
def test_the_role_query_reads_as_intended(row: tuple[Any, ...] | None, expected: bool) -> None:
    conn = _Conn(row)
    assert role_bypasses_rls(conn) is expected
    assert conn.cursor_obj.executed == [
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ]
    assert conn.cursor_obj.closed, "the cursor must be released even on the happy path"


def test_the_guard_is_wired_to_the_postgres_engines_first_connection() -> None:
    engine = make_rls_engine("postgresql+psycopg://u:p@localhost:5432/db", pool_size=1)
    assert not engine.pool.dispatch.first_connect
    guard_rls_engine_role(engine)
    assert engine.pool.dispatch.first_connect, "no first_connect listener was attached"


def test_sqlite_has_no_roles_and_is_left_alone() -> None:
    """Community runs on SQLite (or the embedded Postgres, guarded there as well)."""
    engine = create_engine("sqlite://")
    guard_rls_engine_role(engine)
    assert not engine.pool.dispatch.first_connect
    verify_rls_engine_role(engine)  # a no-op, never a query against pg_roles


def test_make_engine_prefers_the_app_url_and_attaches_the_guard() -> None:
    """The dispatch engine keeps the privileged URL; the owner-scoped one gets its own."""
    config = ConnectorConfig(
        edition="cloud",
        database_url="postgresql+psycopg://persona:p@localhost:5432/db",
        app_database_url="postgresql+psycopg://persona_app:p@localhost:5432/db",
    )
    comp = ConnectorComposition(config)
    rls_engine = comp.make_engine()
    dispatch_engine = comp.make_dispatch_engine()
    assert rls_engine.url.username == "persona_app"
    assert dispatch_engine.url.username == "persona"
    assert rls_engine.pool.dispatch.first_connect


def test_make_engine_falls_back_to_the_dispatch_url_but_still_guards_it() -> None:
    """A deployment that has not split its URLs yet keeps working, unless the shared
    role bypasses RLS, in which case the guard refuses it on first use."""
    config = ConnectorConfig(
        edition="cloud", database_url="postgresql+psycopg://persona_app:p@localhost:5432/db"
    )
    engine = ConnectorComposition(config).make_engine()
    assert engine.url.username == "persona_app"
    assert engine.pool.dispatch.first_connect
