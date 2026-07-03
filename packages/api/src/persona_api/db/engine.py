"""Engine factory and the RLS session helper (spec 07, T06, D-07-5).

The hosted service connects with a non-superuser role under RLS. Every
request-scoped DB access must run inside a transaction where the current
user id has been set on the ``app.current_user_id`` session GUC, which the
per-table policies (``rls.py``) read.

The user id is set with ``set_config('app.current_user_id', :uid, true)`` —
NOT ``SET LOCAL app.current_user_id = :uid``, which is a syntax error with a
bound parameter (``SET`` is a utility statement that rejects placeholders;
research §6). ``set_config(..., is_local => true)`` is a normal, parameterised,
injection-safe function call that is transaction-local (auto-clears on
commit/rollback) — exactly the RLS contract.

Spec 08's request middleware composes :func:`rls_connection`: open it with the
authenticated user id for the duration of the request, hand the connection to
the route/store, commit on success or roll back on error.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection, Engine

__all__ = ["create_db_engine", "rls_connection", "set_current_user"]

_SET_USER_SQL = text("SELECT set_config('app.current_user_id', :uid, true)")


def _sync_url(url: str) -> str:
    """Coerce a stray async DSN to the sync psycopg3 dialect (D-07-1)."""
    return url.replace("+asyncpg", "+psycopg")


def create_db_engine(url: str | None = None) -> Engine:
    """Build a synchronous SQLAlchemy engine.

    Args:
        url: The Postgres DSN. Falls back to the ``DATABASE_URL`` env var.
            An async (``+asyncpg``) DSN is coerced to sync (``+psycopg``).

    Raises:
        RuntimeError: If no URL is given and ``DATABASE_URL`` is unset.
    """
    resolved = url or os.environ.get("DATABASE_URL")
    if not resolved:
        msg = "no database URL provided and DATABASE_URL is unset"
        raise RuntimeError(msg)
    return create_engine(_sync_url(resolved))


def set_current_user(connection: Connection, user_id: str) -> None:
    """Set the RLS user GUC on ``connection`` for the current transaction.

    Must be called inside an open transaction; the setting is transaction-local
    and auto-clears at commit/rollback. The value is bound, never interpolated.

    Community SQLite (D-33-X-community-engine): the single-owner engine
    satisfies the ``rls_engine`` contract WITHOUT multi-tenant GUC scoping —
    ``set_config`` does not exist on SQLite and scoping is by owner-id
    predicate. The GUC bind is a Postgres-only defence layer, so it no-ops on
    the sqlite dialect rather than erroring every ``rls_connection`` caller
    (tasks/schedules/jobs/approvals) out of the community edition.
    """
    if connection.dialect.name == "sqlite":
        return
    connection.execute(_SET_USER_SQL, {"uid": user_id})


@contextmanager
def rls_connection(engine: Engine, user_id: str) -> Iterator[Connection]:
    """Yield a connection in a transaction with ``app.current_user_id`` set.

    The contract spec 08's middleware uses: everything done with the yielded
    connection runs under the tenant's RLS scope. Commits on clean exit, rolls
    back on exception.
    """
    with engine.begin() as connection:
        set_current_user(connection, user_id)
        yield connection
