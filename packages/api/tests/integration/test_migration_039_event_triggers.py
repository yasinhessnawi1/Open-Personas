"""Migration 039 (the A7 event-trigger registry) round-trip test (Spec A7, A7-D-5).

Proves the additive table lands with RLS FORCEd and reverses cleanly:

- ``event_triggers`` created, RLS ENABLE + FORCE + the ``user_isolation`` policy present;
- downgrade drops the table and its policy.

Mirrors the migration-032/038 programmatic Alembic pattern (cwd-independent, reset schema at start
AND end, the deployed-DB path reproduced faithfully). Placeholder numbering: authored as 039 off
``038_initiative``; the orchestrator renumbers at merge-back.
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
_PRED = "038_initiative"  # 039 placeholder predecessor (renumbered at merge-back)
_TABLE = "event_triggers"


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


# A from-scratch 001→039 chain is legitimately longer than the 120s "stuck test" backstop.
@pytest.mark.timeout(300)
def test_039_creates_the_registry_with_rls_and_indexes_then_downgrades(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)

    # UP: the table lands with RLS FORCEd + the user_isolation policy + both match/unlink indexes.
    command.upgrade(cfg, "head")
    assert _TABLE in _tables(clean_db)
    assert _rls_forced(clean_db, _TABLE)
    assert _has_policy(clean_db, _TABLE, "user_isolation")
    engine = create_engine(clean_db)
    try:
        index_names = {ix["name"] for ix in inspect(engine).get_indexes(_TABLE)}
    finally:
        engine.dispose()
    assert "idx_event_triggers_owner_kind_enabled" in index_names  # the match hot path
    assert "idx_event_triggers_owner_platform" in index_names  # unlink hygiene

    # DOWN: the table and its policy are gone (reversible — criterion 9).
    command.downgrade(cfg, _PRED)
    assert _TABLE not in _tables(clean_db)
    assert not _has_policy(clean_db, _TABLE, "user_isolation")
