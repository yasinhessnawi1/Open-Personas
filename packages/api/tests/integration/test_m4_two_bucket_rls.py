"""Cross-tenant RLS isolation for the M4 two-bucket tables (Spec M4, T1a).

``payg_grants`` + ``subscription`` are RLS-scoped by ``user_id`` (ENABLE + FORCE +
``user_isolation`` policy, migration 051). Under the non-superuser ``persona_app``
role each tenant sees ONLY its own rows; a cross-tenant row is invisible (not a
403 — it simply does not exist under the caller's scope). Mirrors the
``test_a5_initiative_stores`` app-engine RLS pattern.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

# ``migrated_engine`` (superuser) builds the schema + RLS policies + persona_app grants;
# it is a dependency-ordering fixture param on every test below.
# ruff: noqa: ARG001


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_user(engine: Engine, uid: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": uid, "e": f"{uid}@example.com"},
        )


def _add_payg_as(engine: Engine, uid: str, source_key: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": uid})
        conn.execute(
            text(
                "INSERT INTO payg_grants "
                "(user_id, credits_total, credits_remaining, expires_at, source_billing_key) "
                "VALUES (:u, 100, 100, :exp, :sk)"
            ),
            {"u": uid, "exp": datetime.now(UTC) + timedelta(days=365), "sk": source_key},
        )


def _add_subscription_as(engine: Engine, uid: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": uid})
        conn.execute(text("INSERT INTO subscription (user_id) VALUES (:u)"), {"u": uid})


def _visible_count_as(engine: Engine, uid: str, table: str) -> int:
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": uid})
        # table name is a fixed literal from the test, never user input.
        return int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608


def test_payg_grants_are_tenant_isolated(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a")
    _seed_user(migrated_engine, "user_b")
    _add_payg_as(app_engine, "user_a", "a_lot")

    assert _visible_count_as(app_engine, "user_a", "payg_grants") == 1  # owner sees its own
    assert _visible_count_as(app_engine, "user_b", "payg_grants") == 0  # cross-tenant invisible


def test_subscription_is_tenant_isolated(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a")
    _seed_user(migrated_engine, "user_b")
    _add_subscription_as(app_engine, "user_a")

    assert _visible_count_as(app_engine, "user_a", "subscription") == 1
    assert _visible_count_as(app_engine, "user_b", "subscription") == 0


def test_payg_insert_for_another_tenant_is_rejected(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The WITH CHECK clause forbids writing a row for a different ``user_id`` than
    the caller's scope (no cross-tenant row injection)."""
    from sqlalchemy.exc import ProgrammingError

    _seed_user(migrated_engine, "user_a")
    _seed_user(migrated_engine, "user_b")
    with pytest.raises(ProgrammingError), app_engine.begin() as conn:  # noqa: PT012
        conn.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": "user_a"})
        conn.execute(
            text(
                "INSERT INTO payg_grants "
                "(user_id, credits_total, credits_remaining, expires_at, source_billing_key) "
                "VALUES ('user_b', 100, 100, now() + interval '365 days', 'x_cross')"
            )
        )
