"""Refuse an RLS engine whose role Postgres exempts from row-level security (R9-123).

Every tenant table is ``FORCE ROW LEVEL SECURITY`` (spec 07 D-07-5), which subjects even
the table owner to policy. Postgres still exempts two kinds of role from every policy:
superusers and roles with ``BYPASSRLS``. The connectors process ran its owner-scoped
engine as the ``persona`` superuser, so ``list_personas`` under ``owner_scope`` returned
every persona in the system, the Telegram roster listed three JARVIS rows from three
accounts, and a turn addressed to another tenant's persona failed only because the
``conversations`` foreign key happened to reject the pair. Nothing logged, nothing
refused: the scope was set, the role just did not honour it.

The check runs on the engine's FIRST connection (``first_connect``), the earliest point
at which the role is knowable, and can be forced at startup through
:func:`verify_rls_engine_role` so a process fails before it serves. SQLite (community)
has no roles and is skipped by dialect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import event

from persona_connectors.errors import ConnectorError

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

__all__ = ["guard_rls_engine_role", "role_bypasses_rls", "verify_rls_engine_role"]

_ROLE_QUERY = "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"

_REFUSAL = (
    "the connectors RLS engine connected as a role that bypasses row-level security "
    "(superuser or BYPASSRLS); point PERSONA_CONNECTORS_APP_DATABASE_URL at the "
    "persona_app role"
)


def role_bypasses_rls(dbapi_connection: Any) -> bool:  # noqa: ANN401 — raw DBAPI connection
    """Whether the connection's role is exempt from row-level security.

    Args:
        dbapi_connection: A raw DBAPI connection (what pool events hand out).

    Returns:
        ``True`` for a superuser or a ``BYPASSRLS`` role, ``False`` otherwise. A role
        absent from ``pg_roles`` reads as ``False``; it cannot be exempt from anything.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(_ROLE_QUERY)
        row = cursor.fetchone()
    finally:
        cursor.close()
    return bool(row and row[0])


def guard_rls_engine_role(engine: Engine) -> None:
    """Attach the role check to ``engine``'s first connection (Postgres only).

    Args:
        engine: The owner-scoped engine built by ``make_rls_engine``.

    Raises:
        ConnectorError: From inside the first connection attempt, when the role
            bypasses RLS. No connection is handed out.
    """
    if engine.dialect.name != "postgresql":
        return

    @event.listens_for(engine, "first_connect")
    def _refuse_bypassing_role(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401 — pool-event sig
        if role_bypasses_rls(dbapi_connection):
            raise ConnectorError(_REFUSAL, context={"engine": "rls", "dialect": "postgresql"})


def verify_rls_engine_role(engine: Engine) -> None:
    """Open one connection eagerly so the first-connect guard runs at startup.

    Args:
        engine: The guarded engine.

    Raises:
        ConnectorError: When the role bypasses RLS (propagated from the guard).
    """
    if engine.dialect.name != "postgresql":
        return
    with engine.connect():
        pass
