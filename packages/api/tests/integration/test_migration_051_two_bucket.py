"""Migration 051 (M4 two-bucket ledger) round-trip test (Spec M4, T1a).

Proves the additive migration lands and reverses cleanly:

- ``credits.allowance_period`` column added (``credits.balance`` UNCHANGED);
- ``payg_grants`` + ``subscription`` created, each with RLS FORCEd + the
  ``user_isolation`` policy + the expected indexes;
- downgrade drops the two tables + their policies + the ``allowance_period`` column.

Mirrors the migration-039/032 programmatic Alembic pattern (cwd-independent, reset
schema at start AND end, the deployed-DB path reproduced faithfully). Placeholder
numbering: authored as 051 off ``050_credit_tx_cost_columns``; renumbered at merge-back.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]  # packages/api
_ALEMBIC_INI = _API_DIR / "alembic.ini"
_PRED = "050_credit_tx_cost_columns"  # 051 predecessor (renumbered at merge-back)


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _reset_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def _tables(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _columns(database_url: str, table: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _indexes(database_url: str, table: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return {ix["name"] for ix in inspect(engine).get_indexes(table)}
    finally:
        engine.dispose()


def _rls_forced(database_url: str, table: str) -> bool:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            return bool(
                conn.execute(
                    text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"),
                    {"t": table},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _has_policy(database_url: str, table: str, policy: str) -> bool:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            return (
                conn.execute(
                    text(
                        "SELECT count(*) FROM pg_policies WHERE tablename = :t AND policyname = :p"
                    ),
                    {"t": table, "p": policy},
                ).scalar_one()
                > 0
            )
    finally:
        engine.dispose()


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    _reset_schema(database_url)
    yield database_url
    _reset_schema(database_url)


# A from-scratch 001→051 chain is legitimately longer than the 120s "stuck test" backstop.
@pytest.mark.timeout(300)
def test_051_adds_two_bucket_schema_with_rls_then_downgrades(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)

    # UP: allowance_period added, the two tables land with RLS FORCEd + policy + indexes.
    command.upgrade(cfg, "head")
    assert "allowance_period" in _columns(clean_db, "credits")
    assert "balance" in _columns(clean_db, "credits")  # unchanged (Option A)
    for tbl in ("payg_grants", "subscription"):
        assert tbl in _tables(clean_db)
        assert _rls_forced(clean_db, tbl)
        assert _has_policy(clean_db, tbl, "user_isolation")
    assert "uq_payg_grants_source_key" in _indexes(clean_db, "payg_grants")
    assert "idx_payg_grants_spendable" in _indexes(clean_db, "payg_grants")
    assert "uq_subscription_stripe_sub" in _indexes(clean_db, "subscription")

    # DOWN: the two tables + their policies + the added column are gone (reversible).
    command.downgrade(cfg, _PRED)
    assert "payg_grants" not in _tables(clean_db)
    assert "subscription" not in _tables(clean_db)
    assert "allowance_period" not in _columns(clean_db, "credits")
    assert "balance" in _columns(clean_db, "credits")  # the parity floor stays intact
