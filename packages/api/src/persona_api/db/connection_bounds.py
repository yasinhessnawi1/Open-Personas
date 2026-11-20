"""Bounds on every api Postgres connection: a hung statement, a pool wait, a dead socket (R9-313).

Without them a database call can hang for as long as the server or the network lets it: a
statement stuck behind a lock nobody releases, or a socket whose peer vanished waiting on the
kernel's two-hour keepalive. A chat turn's cancel and the api's shutdown wait behind it, and a
synchronous call made on the event loop stops the whole process.

Four layers, each for a different failure:

* ``statement_timeout`` (server side, passed in libpq ``options`` so it is the session default
  from the first statement, and ``RESET ALL`` returns to it). Postgres cancels the statement and
  aborts its transaction; SQLAlchemy's ``begin()`` then rolls the whole transaction back, so a
  money write is either all there or not there at all. It counts lock waits, so it must sit
  above the longest ``lock_timeout`` any statement here takes (the erasure backstop's 60 s).
* ``pool_timeout``: how long a checkout waits for a free pooled connection.
* ``connect_timeout``: how long libpq waits to establish a connection.
* TCP keepalives plus ``tcp_user_timeout``: a dead peer is noticed in about a minute, whether
  the socket is idle (keepalives) or has data nobody acknowledges (``tcp_user_timeout``).
  ``statement_timeout`` cannot help there, because the server that would enforce it is gone.

A value already in the DSN wins over these defaults, so an operator can still pin one per
deployment. Alembic is not affected: ``alembic/env.py`` builds its own engine from the DSN.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.engine import make_url

if TYPE_CHECKING:
    from sqlalchemy import Connection

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_KEEPALIVES_COUNT",
    "DEFAULT_KEEPALIVES_IDLE_S",
    "DEFAULT_KEEPALIVES_INTERVAL_S",
    "DEFAULT_POOL_TIMEOUT_S",
    "DEFAULT_STATEMENT_TIMEOUT_MS",
    "DbConnectionBounds",
    "engine_options",
    "lift_statement_timeout",
]

#: Five minutes. Well above the slowest legitimate statement on these engines, so the bound only
#: ever meets a hang:
#:
#: * the longest bounded lock waits: the erasure backstop's 60 s
#:   (``persona_erase.WAIT_LOCK_TIMEOUT_MS``), the account row step's 30 s, forget's and node
#:   delete's 15 s. Each ``lock_timeout`` fires first, so their "busy" handling (55P03) is kept;
#: * a persona delete's cascade (seconds, tens for a very large persona);
#: * the sweeps and the job archive, which work in bounded batches.
#:
#: The statements with no bound by design lift it for their own transaction
#: (:func:`lift_statement_timeout`, or inline in core): the erasure's graph step; the account
#: cascade and the account's store-audit re-key (an unindexed scan of every tenant's events);
#: the backstop's cross-tenant scan; the restart sweep at boot (a cancel there would
#: crash-loop the api); and a consolidation run's wait for the owner's graph lock
#: (``persona.graph.maintenance_lock``), which can meet any of those unbounded holders, so it
#: has no 60 s ceiling.
DEFAULT_STATEMENT_TIMEOUT_MS = 300_000
#: SQLAlchemy's own default, stated so it is visible and configurable.
DEFAULT_POOL_TIMEOUT_S = 30.0
#: Same host, a private network: a healthy connect takes milliseconds.
DEFAULT_CONNECT_TIMEOUT_S = 10
#: First probe after 30 s idle, then every 10 s, dead after 3 misses: about a minute.
DEFAULT_KEEPALIVES_IDLE_S = 30
DEFAULT_KEEPALIVES_INTERVAL_S = 10
DEFAULT_KEEPALIVES_COUNT = 3

#: ``-c statement_timeout=``, ``--statement_timeout=`` and ``--statement-timeout=`` (Postgres
#: reads a dash in a long option's name as an underscore).
_STATEMENT_TIMEOUT_IN_OPTIONS = re.compile(r"statement[-_]timeout\s*=")
#: The DBAPI drivers that hand ``connect_args`` to libpq. Any other (asyncpg, pg8000) would
#: reject or misread these keys, so it gets none.
_LIBPQ_DRIVERS = frozenset({"psycopg", "psycopg_async", "psycopg2", "psycopg2cffi"})
_LIFT_SQL = text("SELECT set_config('statement_timeout', '0', true)")


@dataclass(frozen=True)
class DbConnectionBounds:
    """The connection bounds one engine applies. Zero switches a bound off (not the pool's)."""

    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS
    pool_timeout_s: float = DEFAULT_POOL_TIMEOUT_S
    connect_timeout_s: int = DEFAULT_CONNECT_TIMEOUT_S
    keepalives_idle_s: int = DEFAULT_KEEPALIVES_IDLE_S
    keepalives_interval_s: int = DEFAULT_KEEPALIVES_INTERVAL_S
    keepalives_count: int = DEFAULT_KEEPALIVES_COUNT

    def __post_init__(self) -> None:
        for name in (
            "statement_timeout_ms",
            "connect_timeout_s",
            "keepalives_idle_s",
            "keepalives_interval_s",
            "keepalives_count",
        ):
            if getattr(self, name) < 0:
                msg = f"{name} must be zero (off) or positive"
                raise ValueError(msg)
        if self.pool_timeout_s <= 0:
            msg = "pool_timeout_s must be positive"
            raise ValueError(msg)


def engine_options(url: str, bounds: DbConnectionBounds) -> dict[str, Any]:
    """The ``create_engine`` keyword arguments that apply ``bounds`` to a Postgres ``url``.

    Returns ``{}`` for anything but a libpq driver (the community SQLite engine, asyncpg,
    pg8000), so a caller can splat it unconditionally. A setting the DSN already carries is
    left to the DSN; ``keepalives=0`` there keeps every keepalive key out.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql" or parsed.get_driver_name() not in _LIBPQ_DRIVERS:
        return {}
    in_dsn = set(parsed.query)
    keepalives_off_in_dsn = str(parsed.query.get("keepalives", "")).strip() == "0"
    connect_args: dict[str, Any] = {}

    def _put(key: str, value: object) -> None:
        if key not in in_dsn:
            connect_args[key] = value

    if bounds.connect_timeout_s > 0:
        _put("connect_timeout", bounds.connect_timeout_s)
    if bounds.keepalives_idle_s > 0 and not keepalives_off_in_dsn:
        _put("keepalives", 1)
        _put("keepalives_idle", bounds.keepalives_idle_s)
        _put("keepalives_interval", bounds.keepalives_interval_s)
        _put("keepalives_count", bounds.keepalives_count)
        probing_s = bounds.keepalives_interval_s * bounds.keepalives_count
        verdict_s = bounds.keepalives_idle_s + probing_s
        _put("tcp_user_timeout", verdict_s * 1000)
    options = _options_with_statement_timeout(parsed.query.get("options"), bounds)
    if options is not None:
        connect_args["options"] = options
    return {"pool_timeout": bounds.pool_timeout_s, "connect_args": connect_args}


def _options_with_statement_timeout(
    dsn_options: str | tuple[str, ...] | None, bounds: DbConnectionBounds
) -> str | None:
    """The libpq ``options`` string to pass, or ``None`` to leave libpq's own choice alone.

    ``connect_args`` replaces a key from the DSN, and any ``options`` at all hides the
    ``PGOPTIONS`` environment variable from libpq. So the options libpq would have used (the
    DSN's, else ``PGOPTIONS``, libpq's own precedence) are kept and the timeout appended. When
    those already set ``statement_timeout``, they win and nothing is passed.
    """
    if bounds.statement_timeout_ms <= 0:
        return None
    existing = " ".join(dsn_options) if isinstance(dsn_options, tuple) else (dsn_options or "")
    if not existing:
        existing = os.environ.get("PGOPTIONS", "")
    if _STATEMENT_TIMEOUT_IN_OPTIONS.search(existing):
        return None
    ours = f"-c statement_timeout={bounds.statement_timeout_ms}"
    return f"{existing} {ours}".strip()


def lift_statement_timeout(conn: Connection) -> None:
    """No ``statement_timeout`` for the rest of ``conn``'s open transaction (R9-313).

    For the few statements whose wait or size has no bound by design, where a timeout would
    fail the work every time it is retried instead of letting it finish: the erasure's graph
    step waiting out an owner's consolidation run, the account erasure's owner-wide cascade and
    store-audit re-key, the backstop's cross-tenant scan, and the restart sweep at boot.
    (Consolidation's lock wait does the same inline, in core.) Transaction-local
    (``set_config(..., true)``), so the connection goes back to the pool bounded. A no-op off
    Postgres. Call it inside ``engine.begin()``: outside a transaction it does nothing.
    """
    if conn.dialect.name == "postgresql":
        conn.execute(_LIFT_SQL)
