"""Migration + schema-contract tests (spec 07, T07).

- The Alembic ``001_initial`` migration runs clean against a fresh DB and
  creates all tables, indexes, and RLS policies (acceptance #5).
- The persona-core transport's private ``memory_chunks`` table view agrees with
  the api-owned migrated schema (D-07-2 contract test — the split-home guard).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from persona_api.db.rls import RLS_EXEMPT_TABLES
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]  # packages/api
_ALEMBIC_INI = _API_DIR / "alembic.ini"


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    # script_location in the ini is relative ("alembic"); make it absolute so
    # the test is cwd-independent (CI-safe).
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def test_migration_upgrade_creates_everything(database_url: str) -> None:
    # Fresh schema: drop everything first.
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")

    engine = create_engine(database_url)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert {
        "users",
        "personas",
        "conversations",
        "messages",
        "runs",
        "memory_chunks",
        "turn_logs",
        "rate_limit_buckets",
        "credits",
        "credit_transactions",
        "audit_log",
        # Spec 30 (migration 009): bring-your-own MCP.
        "user_mcp_servers",
        "persona_mcp_assignments",
        "alembic_version",
    }.issubset(tables)

    # HNSW + partial current-heads indexes exist.
    idx = {i["name"] for i in insp.get_indexes("memory_chunks")}
    assert "idx_memory_embedding" in idx
    assert "idx_memory_current_heads" in idx

    # RLS: every tenant table is enabled + forced and has the policy.
    with engine.connect() as conn:
        forced = {
            r[0]
            for r in conn.execute(
                text("SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity")
            )
        }
        policied = {
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_policies WHERE policyname = 'user_isolation'")
            )
        }
    # The invariant is "RLS on every tenant-scoped table" (ENGINEERING_STANDARDS), so ASK THE
    # SCHEMA which tables those are rather than listing the ones we already know about.
    #
    # This assertion used to be `expected.issubset(forced)` over eleven table names from the
    # migration-009 era. It could not fail on the thing it existed to catch: a NEW tenant
    # table with no policy is not in `expected`, and `issubset` never fails on an addition.
    # Forty-two tenant-scoped tables later it was still checking the original eleven.
    with engine.connect() as conn:
        tenant_scoped = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT DISTINCT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t "
                    "  ON t.table_name = c.table_name AND t.table_schema = c.table_schema "
                    "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
                    "  AND c.column_name IN ('owner_id', 'user_id')"
                )
            )
        }
    # Tables scoped through an FK chain instead of a user column of their own — they carry no
    # owner_id, so the discovery above cannot see them, and they must still be covered.
    tenant_scoped |= {"messages", "turn_logs", "memory_chunks"}
    must_have_rls = tenant_scoped - set(RLS_EXEMPT_TABLES)

    assert must_have_rls, "discovery found no tenant-scoped tables — the query is wrong"
    missing_force = sorted(must_have_rls - forced)
    missing_policy = sorted(must_have_rls - policied)
    assert not missing_force, (
        f"tenant-scoped tables without FORCED row-level security: {missing_force}. "
        "Add the ENABLE/FORCE + user_isolation policy in the migration, or add the table to "
        "persona_api.db.rls.RLS_EXEMPT_TABLES with the reason it is not tenant-scoped."
    )
    assert not missing_policy, (
        f"tenant-scoped tables without a user_isolation policy: {missing_policy}. "
        "Forcing RLS without a policy denies ALL rows, which fails closed but silently."
    )
    # The exemptions must be real tables, so a renamed table cannot leave a stale excuse.
    with engine.connect() as conn:
        all_tables = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            )
        }
    stale = sorted(set(RLS_EXEMPT_TABLES) - all_tables)
    assert not stale, f"RLS_EXEMPT_TABLES names tables that no longer exist: {stale}"
    engine.dispose()


def test_migration_downgrade_is_clean(database_url: str) -> None:
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(database_url)
    insp = inspect(engine)
    remaining = [t for t in insp.get_table_names() if t != "alembic_version"]
    assert remaining == []
    engine.dispose()
    # Leave the DB migrated for any later test in the session.
    command.upgrade(cfg, "head")


def test_core_transport_view_matches_migrated_schema() -> None:
    """D-07-2 split-home contract: core's memory_chunks view == api schema.

    ``persona-core`` cannot import the api schema, so it defines its own
    ``memory_chunks`` Table. This asserts the two never drift in the columns the
    transport reads/writes. Pure in-memory comparison (no DB needed).
    """
    from persona.stores.postgres import _memory_chunks as core_view
    from persona_api.db.models import memory_chunks as api_table

    core_cols = {c.name for c in core_view.c}
    api_cols = {c.name for c in api_table.c}
    assert core_cols == api_cols, (
        f"core transport view diverged from api schema: "
        f"core-only={core_cols - api_cols}, api-only={api_cols - core_cols}"
    )
