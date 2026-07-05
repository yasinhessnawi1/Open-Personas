"""Migration 036 (initiative stores + dial) round-trip test (Spec A5, A5-D-X-migration).

Proves the three additive pieces land correctly:

- ``initiative_declines`` / ``initiative_notices`` — RLS ENABLEd + FORCEd, the
  partial LIVE uniques present;
- ``personas.initiative_dial`` (+ ``_updated_at``) — server-default backfill to
  the RATIFIED ``'propose_only'`` (A5-D-5), including on a pre-existing row in
  the deployed-DB path.

Mirrors the migration-032 programmatic Alembic pattern (cwd-independent, reset
schema at start AND end, the deployed-DB path reproduced faithfully so the
guarded create/add-column actually runs). Placeholder numbering: authored as
038 off ``037_notifications_schedule_kind``; the orchestrator renumbers at merge-back.
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
_PRED = "037_notifications_schedule_kind"  # 038 (was 036) placeholder predecessor (renumbered at merge-back)
_TABLES = ("initiative_declines", "initiative_notices")


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


def _persona_columns(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("personas")}
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


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    _reset_schema(database_url)
    yield database_url
    _reset_schema(database_url)


def test_036_creates_tables_rls_and_dial_columns_then_downgrades(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)

    command.upgrade(cfg, "head")
    tables = _tables(clean_db)
    assert set(_TABLES) <= tables
    for table in _TABLES:
        assert _rls_forced(clean_db, table)
    assert {"initiative_dial", "initiative_dial_updated_at"} <= _persona_columns(clean_db)

    command.downgrade(cfg, _PRED)
    tables = _tables(clean_db)
    assert not (set(_TABLES) & tables)
    assert "initiative_dial" not in _persona_columns(clean_db)


def test_036_deployed_db_path_backfills_dial_default(clean_db: str) -> None:
    """The genuine DEPLOYED-DB path: a 034-shaped DB with an EXISTING persona row.

    ``001`` builds the current canonical models, so the pre-A5 shape is reproduced
    (drop the tables + columns, rewind ``alembic_version``); a persona created in
    that state must read ``'propose_only'`` after 036 — the ratified default
    backfill, not NULL (A5-D-5).
    """
    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, _PRED)

    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            for table in reversed(_TABLES):
                conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            conn.execute(text("ALTER TABLE personas DROP COLUMN IF EXISTS initiative_dial"))
            conn.execute(
                text("ALTER TABLE personas DROP COLUMN IF EXISTS initiative_dial_updated_at")
            )
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": _PRED})
            conn.execute(text("INSERT INTO users (id, email) VALUES ('u_36', 'u36@x.test')"))
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p_36', 'u_36', 'name: X')")
            )
    finally:
        engine.dispose()
    assert "initiative_dial" not in _persona_columns(clean_db)

    command.upgrade(cfg, "head")
    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            dial, dial_updated = conn.execute(
                text(
                    "SELECT initiative_dial, initiative_dial_updated_at "
                    "FROM personas WHERE id = 'p_36'"
                )
            ).one()
    finally:
        engine.dispose()
    assert dial == "propose_only"  # the ratified default backfill (A5-D-5)
    assert dial_updated is None  # never changed by hand

    # Idempotent re-run (checkfirst / IF NOT EXISTS): rewind and re-upgrade — no raise.
    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": _PRED})
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")
    assert set(_TABLES) <= _tables(clean_db)


def test_036_partial_unique_allows_history_but_one_live_row(clean_db: str) -> None:
    """The T3-gate-ratified partial uniques, proven at the SQL level: a second LIVE
    row for the same (owner, opportunity) is rejected; a revived/superseded history
    row does NOT block a fresh one."""
    from sqlalchemy.exc import IntegrityError

    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, "head")

    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, email) VALUES ('u_pu', 'upu@x.test')"))
            conn.execute(
                text(
                    "INSERT INTO initiative_declines "
                    "(id, owner_id, opportunity_key, trigger, source) "
                    "VALUES ('d1', 'u_pu', 'k1', 'conflict', 'declined_reply')"
                )
            )
        # A second LIVE decline for the same (owner, key) conflicts.
        with pytest.raises(IntegrityError), engine.begin() as conn:  # noqa: PT012
            conn.execute(
                text(
                    "INSERT INTO initiative_declines "
                    "(id, owner_id, opportunity_key, trigger, source) "
                    "VALUES ('d2', 'u_pu', 'k1', 'conflict', 'stop_verb')"
                )
            )
        # Revive the first (history row) → a fresh LIVE decline is insertable.
        with engine.begin() as conn:
            conn.execute(text("UPDATE initiative_declines SET revived_at = now()"))
            conn.execute(
                text(
                    "INSERT INTO initiative_declines "
                    "(id, owner_id, opportunity_key, trigger, source) "
                    "VALUES ('d3', 'u_pu', 'k1', 'conflict', 'declined_reply')"
                )
            )
    finally:
        engine.dispose()
