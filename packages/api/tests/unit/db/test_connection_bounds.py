"""R9-313: every api Postgres engine bounds a hung statement, a pool wait and a dead socket.

A database call with no bound holds up a chat turn's cancel and the api's shutdown for as long
as it hangs. The engines now pass libpq a server-side ``statement_timeout`` (in ``options``), a
``connect_timeout`` and TCP keepalives, and the pool a ``pool_timeout``.

No database here: ``do_connect`` hands us the exact arguments psycopg would be called with,
and the test stops the connect before it reaches the network.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona_api.config import APIConfig
from persona_api.db.connection_bounds import (
    DEFAULT_STATEMENT_TIMEOUT_MS,
    DbConnectionBounds,
    engine_options,
    lift_statement_timeout,
)
from persona_api.db.engine import create_db_engine
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import Engine, event

# A fixture DSN: nothing listens there, and the connect is stopped before it is tried.
_URL = "postgresql+psycopg://app@db.invalid:5432/persona"


class _StopConnect(Exception):  # noqa: N818 - a control-flow sentinel, not an error
    pass


def _connect_params(engine: Engine) -> dict[str, Any]:
    """The keyword arguments the engine would hand ``psycopg.connect``."""
    seen: dict[str, Any] = {}

    @event.listens_for(engine, "do_connect")
    def _capture(_dialect: Any, _rec: Any, _cargs: Any, cparams: dict[str, Any]) -> None:  # noqa: ANN401
        seen.update(cparams)
        raise _StopConnect

    with pytest.raises(_StopConnect):
        engine.connect()
    engine.dispose()
    return seen


def test_defaults_bound_every_connection() -> None:
    opts = engine_options(_URL, DbConnectionBounds())
    args = opts["connect_args"]
    assert opts["pool_timeout"] == DbConnectionBounds().pool_timeout_s
    assert args["options"] == f"-c statement_timeout={DEFAULT_STATEMENT_TIMEOUT_MS}"
    assert args["connect_timeout"] == DbConnectionBounds().connect_timeout_s
    assert args["keepalives"] == 1
    assert args["keepalives_idle"] == DbConnectionBounds().keepalives_idle_s
    assert args["keepalives_interval"] == DbConnectionBounds().keepalives_interval_s
    assert args["keepalives_count"] == DbConnectionBounds().keepalives_count


def test_the_tcp_user_timeout_matches_the_keepalive_verdict() -> None:
    bounds = DbConnectionBounds(keepalives_idle_s=20, keepalives_interval_s=5, keepalives_count=4)
    args = engine_options(_URL, bounds)["connect_args"]
    # Unacknowledged data is given up on no sooner than an idle socket would be.
    assert args["tcp_user_timeout"] == (20 + 5 * 4) * 1000


def test_the_default_statement_timeout_clears_the_longest_lock_wait() -> None:
    """The longest lock wait any api statement is allowed (the erasure backstop, 60 s)."""
    from persona_api.jobs.handlers.persona_erase import WAIT_LOCK_TIMEOUT_MS

    assert DEFAULT_STATEMENT_TIMEOUT_MS >= 2 * WAIT_LOCK_TIMEOUT_MS


def test_zero_switches_each_bound_off() -> None:
    bounds = DbConnectionBounds(statement_timeout_ms=0, connect_timeout_s=0, keepalives_idle_s=0)
    args = engine_options(_URL, bounds)["connect_args"]
    assert "options" not in args
    assert "connect_timeout" not in args
    assert "keepalives" not in args
    assert "tcp_user_timeout" not in args


def test_options_already_in_the_dsn_are_kept() -> None:
    url = f"{_URL}?options=-c%20search_path%3Dpublic"
    args = engine_options(url, DbConnectionBounds(statement_timeout_ms=5000))["connect_args"]
    assert args["options"] == "-c search_path=public -c statement_timeout=5000"


def test_a_statement_timeout_set_in_the_dsn_wins() -> None:
    url = f"{_URL}?options=-c%20statement_timeout%3D1000"
    args = engine_options(url, DbConnectionBounds())["connect_args"]
    assert "options" not in args  # the DSN's own value reaches libpq untouched


def test_a_libpq_setting_in_the_dsn_wins() -> None:
    url = f"{_URL}?connect_timeout=3&keepalives_idle=7"
    args = engine_options(url, DbConnectionBounds())["connect_args"]
    assert "connect_timeout" not in args
    assert "keepalives_idle" not in args
    assert args["keepalives"] == 1


def test_sqlite_gets_no_postgres_options() -> None:
    assert engine_options("sqlite+pysqlite:///x.db", DbConnectionBounds()) == {}


@pytest.mark.parametrize(
    "field",
    [
        "statement_timeout_ms",
        "connect_timeout_s",
        "keepalives_idle_s",
        "keepalives_interval_s",
        "keepalives_count",
    ],
)
def test_negative_bounds_are_refused(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        DbConnectionBounds(**{field: -1})


def test_a_pool_timeout_must_be_positive() -> None:
    with pytest.raises(ValueError, match="pool_timeout_s"):
        DbConnectionBounds(pool_timeout_s=0)


def test_the_rls_engine_hands_psycopg_the_bounds() -> None:
    bounds = DbConnectionBounds(statement_timeout_ms=45_000, pool_timeout_s=7)
    engine = make_rls_engine(_URL, pool_size=2, bounds=bounds)
    assert engine.pool.timeout() == 7  # type: ignore[attr-defined]
    params = _connect_params(engine)
    assert params["options"] == "-c statement_timeout=45000"
    assert params["keepalives"] == 1
    assert params["connect_timeout"] == bounds.connect_timeout_s


def test_the_rls_engine_is_bounded_when_the_caller_passes_nothing() -> None:
    """The worker and the standalone connectors build it without bounds: they get the defaults."""
    params = _connect_params(make_rls_engine(_URL))
    assert params["options"] == f"-c statement_timeout={DEFAULT_STATEMENT_TIMEOUT_MS}"


def test_the_platform_engine_hands_psycopg_the_bounds() -> None:
    engine = create_db_engine(_URL, bounds=DbConnectionBounds(statement_timeout_ms=90_000))
    params = _connect_params(engine)
    assert params["options"] == "-c statement_timeout=90000"
    assert params["keepalives"] == 1


def test_the_platform_engine_is_bounded_when_the_caller_passes_nothing() -> None:
    params = _connect_params(create_db_engine(_URL))
    assert params["options"] == f"-c statement_timeout={DEFAULT_STATEMENT_TIMEOUT_MS}"


def test_config_defaults_match_the_bounds_defaults() -> None:
    config = APIConfig()
    assert config.db_connection_bounds == DbConnectionBounds()


def test_config_reads_the_bounds_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_API_DB_STATEMENT_TIMEOUT_MS", "150000")
    monkeypatch.setenv("PERSONA_API_DB_POOL_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("PERSONA_API_DB_CONNECT_TIMEOUT_SECONDS", "4")
    monkeypatch.setenv("PERSONA_API_DB_KEEPALIVES_IDLE_SECONDS", "15")
    monkeypatch.setenv("PERSONA_API_DB_KEEPALIVES_INTERVAL_SECONDS", "6")
    monkeypatch.setenv("PERSONA_API_DB_KEEPALIVES_COUNT", "2")
    bounds = APIConfig().db_connection_bounds
    assert bounds == DbConnectionBounds(
        statement_timeout_ms=150_000,
        pool_timeout_s=12.5,
        connect_timeout_s=4,
        keepalives_idle_s=15,
        keepalives_interval_s=6,
        keepalives_count=2,
    )


def test_config_refuses_a_negative_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv("PERSONA_API_DB_STATEMENT_TIMEOUT_MS", "-5")
    with pytest.raises(ValidationError):
        APIConfig()


class _FakeConn:
    def __init__(self, dialect: str) -> None:
        self.dialect = type("_D", (), {"name": dialect})()
        self.sql: list[str] = []

    def execute(self, statement: object) -> None:
        self.sql.append(str(statement))


def test_lifting_the_bound_is_transaction_local_on_postgres() -> None:
    conn = _FakeConn("postgresql")
    lift_statement_timeout(conn)  # type: ignore[arg-type]
    assert conn.sql == ["SELECT set_config('statement_timeout', '0', true)"]


def test_lifting_the_bound_is_a_no_op_off_postgres() -> None:
    conn = _FakeConn("sqlite")
    lift_statement_timeout(conn)  # type: ignore[arg-type]
    assert conn.sql == []
