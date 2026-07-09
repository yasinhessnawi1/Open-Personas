"""RLS pool listeners tolerate invalidated connections (R9-006).

From an owner outage log (DB endpoint unreachable): SQLAlchemy fires the pool
``checkin`` event for an INVALIDATED record — ``dbapi_connection=None`` — after a
failed checkout. The listener then crashed (``AttributeError: 'NoneType' object
has no attribute 'cursor'``), MASKING the real ``OperationalError``. The fix:
``None``/dead-connection paths are no-ops (nothing to scope, nothing to leak),
while the fail-closed semantics on a LIVE connection are unchanged — a checkout
that cannot set the GUC still raises, never silently serving an unscoped
connection.
"""

from __future__ import annotations

import pytest
from persona_api.middleware.rls_context import (
    _reset_connection,  # noqa: PLC2701 — the listener bodies under test
    _scope_connection,  # noqa: PLC2701
    current_user_id,
)


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn
        self.closed = False

    def execute(self, sql: str, params: tuple[str, ...] | None = None) -> None:
        if self._conn.execute_error is not None:
            raise self._conn.execute_error
        self._conn.executed.append((sql, params))

    def close(self) -> None:
        self.closed = True


class _FakeConn:
    """A minimal psycopg3-shaped DBAPI connection double."""

    def __init__(self, *, closed: bool = False, execute_error: Exception | None = None) -> None:
        self.closed = closed
        self.execute_error = execute_error
        self.executed: list[tuple[str, tuple[str, ...] | None]] = []
        self.committed = False
        self.cursors: list[_FakeCursor] = []

    def cursor(self) -> _FakeCursor:
        cursor = _FakeCursor(self)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.committed = True


# --- checkin (_reset_connection) ---------------------------------------------


def test_checkin_with_none_connection_is_a_noop() -> None:
    # The outage shape: an invalidated record checks in with dbapi_connection=None.
    _reset_connection(None)  # must not raise (would mask the real OperationalError)


def test_checkin_with_closed_connection_is_a_noop() -> None:
    conn = _FakeConn(closed=True)
    _reset_connection(conn)
    assert conn.executed == [], "no SQL on a dead connection"
    assert not conn.committed


def test_checkin_on_live_connection_resets_guc_and_commits() -> None:
    conn = _FakeConn()
    _reset_connection(conn)
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "set_config('app.current_user_id', ''" in sql, "reset to '' (no residue)"
    assert params is None
    assert conn.committed
    assert all(c.closed for c in conn.cursors)


def test_checkin_reset_failure_on_live_connection_still_propagates() -> None:
    # A LIVE connection whose reset fails must NOT be silently returned to the
    # pool as clean — the error propagates (SQLAlchemy invalidates it).
    conn = _FakeConn(execute_error=RuntimeError("reset failed"))
    with pytest.raises(RuntimeError, match="reset failed"):
        _reset_connection(conn)
    assert all(c.closed for c in conn.cursors), "cursor closed even on failure"


# --- checkout (_scope_connection) ----------------------------------------------


def test_checkout_with_none_connection_is_a_noop() -> None:
    _scope_connection(None)  # nothing to scope; nothing can be served from it


def test_checkout_on_live_connection_sets_guc_from_contextvar() -> None:
    conn = _FakeConn()
    token = current_user_id.set("user_a")
    try:
        _scope_connection(conn)
    finally:
        current_user_id.reset(token)
    assert len(conn.executed) == 1
    sql, params = conn.executed[0]
    assert "set_config('app.current_user_id', %s" in sql
    assert params == ("user_a",)
    assert all(c.closed for c in conn.cursors)


def test_checkout_with_no_user_scopes_empty_fail_closed() -> None:
    conn = _FakeConn()
    assert current_user_id.get() is None  # no request scope bound
    _scope_connection(conn)
    _, params = conn.executed[0]
    assert params == ("",), "absent uid scopes to '' → RLS matches no rows (fail-closed)"


def test_checkout_failure_on_live_connection_still_propagates() -> None:
    # THE fail-closed invariant (D-08-1): a live connection the GUC cannot be set
    # on must never be handed to the request unscoped.
    conn = _FakeConn(execute_error=RuntimeError("db gone"))
    token = current_user_id.set("user_a")
    try:
        with pytest.raises(RuntimeError, match="db gone"):
            _scope_connection(conn)
    finally:
        current_user_id.reset(token)
