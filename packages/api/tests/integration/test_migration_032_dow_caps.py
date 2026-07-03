"""Migration 032 (day_spend + inflight_ops) round-trip test (Spec R7, R7-D-7).

Proves the two denial-of-wallet cross-session stores land correctly:

- ``day_spend`` — per-user per-UTC-day spend counter, PK ``(user_id, utc_day)``,
  ``spent >= 0`` CHECK, RLS-scoped by ``user_id``.
- ``inflight_ops`` — durable in-flight registry for long ops, RLS-scoped by
  ``user_id``, indexed on ``(user_id, op_class)``.

Mirrors the migration-025 programmatic Alembic pattern (cwd-independent). Split-home:
``001_initial`` builds both via ``metadata.create_all`` from the canonical models,
so ``032``'s ``Table.create(checkfirst=True)`` is a no-op on a fresh DB; the
DEPLOYED-DB path (tables absent, version rewound) is reproduced faithfully so the
guarded create actually runs. Each test resets the schema at start AND end.
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
_PRED = "031_request_telemetry"  # 032's predecessor (renumbered at merge-back)


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


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    _reset_schema(database_url)
    yield database_url
    _reset_schema(database_url)


def test_032_creates_and_removes_dow_cap_tables(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)

    command.upgrade(cfg, "head")
    tables = _tables(clean_db)
    assert "day_spend" in tables
    assert "inflight_ops" in tables
    # RLS FORCEd (so even the table owner is subject to policy — production isolation).
    assert _rls_forced(clean_db, "day_spend")
    assert _rls_forced(clean_db, "inflight_ops")

    # downgrade 032 -> 031: both tables dropped.
    command.downgrade(cfg, _PRED)
    tables = _tables(clean_db)
    assert "day_spend" not in tables
    assert "inflight_ops" not in tables


def test_032_idempotent_on_deployed_db(clean_db: str) -> None:
    """The genuine DEPLOYED-DB path: a Postgres that has the 031 schema but NOT
    yet ``day_spend`` / ``inflight_ops``. The split-home harness can't reach that
    by stopping earlier (``001`` builds both from the current canonical models), so
    we reproduce it: upgrade to 031, DROP both tables, rewind ``alembic_version``,
    then run 032 — the guarded create actually creates them, and a second run is a
    no-op (checkfirst)."""
    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, _PRED)

    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS inflight_ops CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS day_spend CASCADE"))
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": _PRED})
    finally:
        engine.dispose()
    assert "day_spend" not in _tables(clean_db)

    # Apply 032 on the deployed-shaped DB: both created.
    command.upgrade(cfg, "head")
    assert {"day_spend", "inflight_ops"} <= _tables(clean_db)

    # Idempotent: re-running the create step (checkfirst) does not raise. Rewind
    # the version and re-upgrade — the tables already exist, so it's a clean no-op.
    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": _PRED})
    finally:
        engine.dispose()
    command.upgrade(cfg, "head")  # must not raise on already-present tables
    assert {"day_spend", "inflight_ops"} <= _tables(clean_db)


def test_day_spend_shape_pk_and_nonneg_check(clean_db: str) -> None:
    """PK ``(user_id, utc_day)`` (one row per user per UTC day) + ``spent >= 0``
    CHECK (the durable floor — refunds reverse the balance, never the day counter)."""
    from sqlalchemy.exc import IntegrityError

    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, "head")

    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, email) VALUES ('u_ds', 'uds@x.test')"))
            conn.execute(
                text(
                    "INSERT INTO day_spend (user_id, utc_day, spent) "
                    "VALUES ('u_ds', DATE '2026-07-03', 100)"
                )
            )
        # Duplicate (user_id, utc_day) rejected by the composite PK.
        with pytest.raises(IntegrityError), engine.begin() as conn:  # noqa: PT012
            conn.execute(
                text(
                    "INSERT INTO day_spend (user_id, utc_day, spent) "
                    "VALUES ('u_ds', DATE '2026-07-03', 5)"
                )
            )
        # spent < 0 rejected by the CHECK.
        with pytest.raises(IntegrityError), engine.begin() as conn:  # noqa: PT012
            conn.execute(
                text(
                    "INSERT INTO day_spend (user_id, utc_day, spent) "
                    "VALUES ('u_ds', DATE '2026-07-04', -1)"
                )
            )
    finally:
        engine.dispose()
