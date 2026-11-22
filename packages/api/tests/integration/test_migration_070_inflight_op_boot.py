"""Migration 070 (``inflight_ops.instance_id`` and ``boot_id``) round trip, on Postgres (R9-318).

On a fresh database ``001_initial`` builds both columns from the model, so 070's upgrade is a
no-op there. Here the schema goes to head, back to ``069`` (the columns dropped), takes a slot
written by pre-070 code, and comes up again: the real upgrade runs, the old slot has no instance
or boot id, and the next boot's sweep frees it.

Each test resets the schema at start and end so the shared integration DB is left clean.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from persona.concurrency import release_previous_boot_long_ops
from sqlalchemy import create_engine, inspect, text

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]  # packages/api
_BEFORE = "069_persona_create_complete"
_OWNER = "u_070"
_COLUMNS = {"instance_id", "boot_id"}


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _reset_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def _columns(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("inflight_ops")}
    finally:
        engine.dispose()


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    _reset_schema(database_url)
    yield database_url
    _reset_schema(database_url)


def test_070_round_trip_and_a_pre_070_slot_is_freed_by_the_next_boot(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, "head")
    assert _columns(clean_db) >= _COLUMNS
    command.downgrade(cfg, _BEFORE)
    assert not (_COLUMNS & _columns(clean_db)), "069 has neither column"

    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:  # a slot as pre-070 code writes it
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, 'u070@x.test')"), {"u": _OWNER}
            )
            conn.execute(
                text("INSERT INTO inflight_ops (id, user_id, op_class) VALUES ('old', :u, 'chat')"),
                {"u": _OWNER},
            )
        command.upgrade(cfg, "head")  # 070's ADD COLUMNs, this time not a no-op
        assert _columns(clean_db) >= _COLUMNS
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT instance_id, boot_id FROM inflight_ops WHERE id = 'old'")
            ).one()
        assert tuple(row) == (None, None)
        with engine.begin() as conn:
            assert release_previous_boot_long_ops(conn=conn, instance_id="api") == 1
        with engine.connect() as conn:
            left = conn.execute(text("SELECT count(*) FROM inflight_ops")).scalar_one()
        assert left == 0
    finally:
        engine.dispose()
