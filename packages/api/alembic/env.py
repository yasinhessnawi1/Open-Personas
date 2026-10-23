"""Alembic migration environment — SYNCHRONOUS runner (spec 07, D-07-6).

The persona-core Postgres transport is sync (psycopg3, D-07-1), so migrations
use the standard synchronous Alembic runner with the ``postgresql+psycopg://``
dialect — not the async runner. The DB URL comes from the ``DATABASE_URL`` env
var when set (CI / local Docker), falling back to the ``alembic.ini`` default.

``target_metadata`` is the canonical Core schema from ``persona_api.db.models``
so ``--autogenerate`` stays sane, though ``001_initial`` writes explicit SQL.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from persona_api.db.models import metadata as target_metadata
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Let DATABASE_URL win over the ini default; coerce a stray async DSN to sync. The ini
# value goes through configparser interpolation, where "%" is special, so a
# percent-encoded DSN (a Windows socket folder, a password with "%") is escaped.
_env_url = os.environ.get("DATABASE_URL")
if _env_url:
    config.set_main_option(
        "sqlalchemy.url", _env_url.replace("+asyncpg", "+psycopg").replace("%", "%%")
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DB connection (``alembic upgrade --sql``)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB via a sync engine."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
