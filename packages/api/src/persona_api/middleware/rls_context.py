"""Structural RLS connection scoping (spec 08, T05, D-08-1).

THE load-bearing security invariant of the whole spec, settled by the Phase-3
spike (research §1). Restated for any future reader (a launch audit, a new
contributor):

> RLS-scoping is a **property of the per-request engine's connection pool**, not
> a call any route makes — so it cannot be forgotten. A ``checkout`` pool
> listener runs ``set_config('app.current_user_id', <uid or ''>, false)`` on
> **every** connection the engine hands out, reading the uid from a
> request-scoped :data:`current_user_id` ``ContextVar``. Therefore every
> connection the request touches — the route's own queries AND the runtime
> store's ``engine.begin()``/``connect()`` calls (``PostgresBackend`` opens its
> own transactions, [postgres.py]) — is scoped before any SQL runs. A
> ``checkin`` listener resets the GUC to ``''`` so **no tenant id residue
> survives to the next checkout** (the leak-prevention invariant; proven under
> ``pool_size=1`` reuse in research §1, Q2). An absent/empty uid yields
> ``current_setting(...) IS NULL`` → matches no RLS policy row → **fail-closed**
> (zero rows, never a leak; reproduced as the spike's negative control).

The single moving part is the :data:`current_user_id` ``ContextVar``: it is set
in exactly one place — the auth dependency (:mod:`persona_api.auth.deps`) at the
start of each request — and reset at request end. Its per-request / per-task
isolation (no bleed across concurrent ``asyncio`` tasks) is regression-tested
(research §5 confirmed it under ``asyncio.gather``).

``PostgresBackend`` is **left untouched** (mechanism (a), not "inject a
connection" (b)) — no spec-07 surgery, no risk to the shipped store tests.

``set_config(..., false)`` (session-scoped, not ``true``/transaction-local) is
used here because the value is set at *checkout*, before SQLAlchemy begins the
transaction; the checkin reset is what bounds its lifetime to the request. This
was verified in the spike (Q1: the value survives into the request's
transaction; Q2: it never leaks to the next checkout).
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator  # runtime: appears in a FastAPI dep signature
from typing import TYPE_CHECKING, Any

from fastapi import Request  # runtime: FastAPI must recognise the Request param
from sqlalchemy import Connection, create_engine, event  # Connection: runtime dep annotation

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "current_user_id",
    "get_rls_connection",
    "make_rls_engine",
]

# The one moving part: the request's authenticated user id. Set by the auth
# dependency, read by the pool listener. Default None → fail-closed.
current_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_user_id", default=None
)

# psycopg3 raw-cursor SQL. set_config is a normal parameterised function call
# (NOT `SET LOCAL ... = :x`, which is a syntax error with a bound param — spec
# 07 surprise #1 / D-07-5). false → session-scoped; the checkin reset bounds it.
_SET_SQL = "SELECT set_config('app.current_user_id', %s, false)"
_RESET_SQL = "SELECT set_config('app.current_user_id', '', false)"


def _scope_connection(dbapi_conn: Any) -> None:  # noqa: ANN401 — DBAPI conn is dynamically typed
    """Checkout body: set the GUC from the contextvar. Fail-closed on a LIVE conn.

    ``dbapi_conn is None`` is a no-op (R9-006): SQLAlchemy fires pool events for an
    **invalidated** record (e.g. after a failed checkout when the DB endpoint is
    unreachable) whose ``dbapi_connection`` is ``None`` — there is no live
    connection to scope, and raising here (``AttributeError: 'NoneType' object has
    no attribute 'cursor'``) would MASK the real ``OperationalError``. On a live
    connection the semantics are unchanged and fail-closed: if the GUC cannot be
    set, the error propagates and the connection is never served unscoped.
    """
    if dbapi_conn is None:
        return
    uid = current_user_id.get()
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute(_SET_SQL, (uid or "",))
    finally:
        cursor.close()


def _reset_connection(dbapi_conn: Any) -> None:  # noqa: ANN401 — DBAPI conn is dynamically typed
    """Checkin body: reset the GUC so no tenant residue survives to the next checkout.

    ``None`` / already-closed connections are no-ops (R9-006): an invalidated pool
    record checks in with ``dbapi_connection=None``, and a dead/closed connection
    has no session state to leak — it will never be handed out again. Raising here
    would mask the outage error that killed the connection. A LIVE connection's
    reset failure still propagates (SQLAlchemy then invalidates the connection, so
    a reset-failed connection is never reused with residue).
    """
    if dbapi_conn is None or getattr(dbapi_conn, "closed", False):
        return
    cursor = dbapi_conn.cursor()
    try:
        cursor.execute(_RESET_SQL)
    finally:
        cursor.close()
    dbapi_conn.commit()


def make_rls_engine(url: str, *, pool_size: int = 5) -> Engine:
    """Build a sync engine whose pool RLS-scopes every connection structurally.

    Every checkout sets ``app.current_user_id`` from :data:`current_user_id`
    (or ``''`` → fail-closed); every checkin resets it. This is the engine the
    request path — route queries AND the runtime's ``PostgresBackend`` stores —
    runs on, so a missed scope is impossible (D-08-1).

    Args:
        url: The Postgres DSN (sync ``postgresql+psycopg://``). In production
            this is the non-superuser ``persona_app`` role (RLS bypasses
            superusers — spec 07 D-07-5).
        pool_size: Connection-pool size. >1 so a slow sync store call doesn't
            serialise concurrent CRUD (research §5).
    """
    engine = create_engine(url, pool_size=pool_size)

    # The DBAPI connection + pool record/proxy are dynamically typed (psycopg3
    # raw connection, SQLAlchemy pool internals); the event-listener signature is
    # fixed by SQLAlchemy, so Any is unavoidable here. The bodies live in the
    # module-level ``_scope_connection`` / ``_reset_connection`` (unit-testable;
    # both tolerate invalidated ``None``/dead connections — R9-006).
    @event.listens_for(engine, "checkout")
    def _set_rls_on_checkout(dbapi_conn: Any, _record: Any, _proxy: Any) -> None:  # noqa: ANN401
        _scope_connection(dbapi_conn)

    @event.listens_for(engine, "checkin")
    def _reset_rls_on_checkin(dbapi_conn: Any, _record: Any) -> None:  # noqa: ANN401 — pool-event sig
        _reset_connection(dbapi_conn)

    return engine


def get_rls_connection(request: Request) -> Iterator[Connection]:
    """FastAPI dependency: an RLS-scoped transactional connection for the request.

    Opens a transaction on the app's RLS engine (``app.state.rls_engine``,
    attached by the lifespan in T10). Because the engine's pool listener set
    ``app.current_user_id`` at checkout from the request-scoped contextvar (set
    by :func:`persona_api.auth.get_current_user`), every statement on this
    connection is tenant-scoped. Commits on clean exit, rolls back on error.

    A route that depends on this MUST also depend on ``get_current_user`` (so the
    contextvar is set first). Routes that skip auth (health, tools/skills lists)
    don't use this dependency.
    """
    engine: Engine = request.app.state.rls_engine
    with engine.begin() as connection:
        yield connection
