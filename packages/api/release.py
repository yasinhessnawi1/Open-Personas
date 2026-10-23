"""Fly ``release_command``: migrate the DB to head + (re)grant ``persona_app``.

Runs once per deploy in the release VM (the built image + the app's secrets),
BEFORE the new version is promoted to serve traffic — and a non-zero exit
**aborts the deploy**, so a failed or missing migration can never ship code
against an un-migrated DB. This replaces the manual ``FLY_DEPLOY.md`` §4b step
(``flyctl ssh console -C "alembic upgrade head"`` + grants), which was easy to
forget — a missed run shipped code querying ``messages.originated`` against a DB
that lacked the column, 500-ing every conversation page.

This is NOT on container *start* (Spec 07 §7's concern — that would re-run on
every machine restart and race across machines): ``release_command`` is a single
deliberate deploy-time step, which honours §7's "explicit, not per-restart" rule
while removing the footgun.

``DATABASE_URL`` is the **superuser** DSN (the request path uses
``APP_DATABASE_URL`` as the non-superuser ``persona_app`` role); migrations
(``CREATE TABLE``/``POLICY``) and the grants both need superuser. Both steps are
idempotent — re-running on a no-new-migration deploy is a clean no-op.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

# packages/api/ — alembic.ini lives here; resolve absolutely so cwd doesn't matter.
_API_DIR = Path(__file__).resolve().parent


def _upgrade_to_head(dsn: str) -> None:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    command.upgrade(cfg, "head")


def _grant_persona_app(dsn: str) -> None:
    """Grant the non-superuser request-path role on every current table/sequence.

    Idempotent belt-and-suspenders matching ``FLY_DEPLOY.md`` §4b: ``ALTER
    DEFAULT PRIVILEGES`` (set at bootstrap) covers future tables, but a bare
    ``GRANT ... ON ALL`` here guarantees the just-applied migrations' tables are
    reachable by ``persona_app`` even if defaults drift. No-op if the role is
    absent (e.g. a non-RLS target).
    """
    engine = create_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'persona_app'")).first():
                conn.execute(
                    text(
                        "GRANT SELECT, INSERT, UPDATE, DELETE "
                        "ON ALL TABLES IN SCHEMA public TO persona_app"
                    )
                )
                conn.execute(
                    text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO persona_app")
                )
    finally:
        engine.dispose()


_PLATFORM_GRANTS = (
    "GRANT USAGE ON SCHEMA public TO persona_platform",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO persona_platform",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO persona_platform",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO persona_platform",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
    "GRANT USAGE, SELECT ON SEQUENCES TO persona_platform",
)


def _grant_persona_platform(dsn: str) -> None:
    """Grant the api's long-running cross-tenant role on every current and future table.

    On the Hetzner box the api process no longer holds the superuser DSN: its
    ``DATABASE_URL`` (the admin, dispatch and JIT-provisioning engines) is the
    ``persona_platform`` role, which bypasses RLS but is NOT a superuser and runs no DDL,
    so a compromised api cannot ``COPY ... TO PROGRAM``, alter roles or read server files.
    Only this release step keeps the superuser DSN. ``deploy/hetzner/bin/restore.sh``
    creates the role; this step keeps its DML grants complete after every migration and
    sets the default privileges, so a table a future migration creates (as the superuser
    running this step) is reachable at once. No-op when the role is absent: the Fly-era
    layout, CI and community are unaffected.
    """
    engine = create_engine(dsn, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            if conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = 'persona_platform'")
            ).first():
                for statement in _PLATFORM_GRANTS:
                    conn.execute(text(statement))
    finally:
        engine.dispose()


def main() -> None:
    dsn = os.environ["DATABASE_URL"]  # superuser DSN; read at runtime, not import
    _upgrade_to_head(dsn)
    _grant_persona_app(dsn)
    _grant_persona_platform(dsn)


if __name__ == "__main__":
    main()
