"""Migration ``066_wellbeing_disclosure_release``: the release columns (D-ORCH-11-18-u).

The user can say a conversation that carried a disclosure may be brought up again. The
release lives on the fact row: ``disclosure_seq`` counts the recorded disclosures,
``released_seq`` is the one the release covers and ``released_at`` is when it was given. A
row deployed under 065 has none of them; the upgrade gives it one disclosure and no release,
so nothing deployed is released by the upgrade.

``001_initial`` builds the table from the model on a fresh database, so the 065 table here is
first brought back to its deployed shape (the three columns and two checks dropped) before
066 runs on it. Postgres only (CI).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _erase_seed import row
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]
_PRED = "065_wellbeing_disclosures"
_HEAD = "066_wellbeing_disclosure_release"
_FAST = {"connect_timeout": 5, "options": "-c lock_timeout=5000 -c statement_timeout=30000"}
_TABLE = "wellbeing_disclosures"
_NEW = ("disclosure_seq", "released_seq", "released_at")
_CHECKS = ("ck_wellbeing_disclosures_release_seq", "ck_wellbeing_disclosures_release_pair")


def _engine(url: str) -> Engine:
    return create_engine(url, connect_args=_FAST)


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _reset_schema(database_url: str) -> None:
    engine = _engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def _execute(url: str, *statements: str) -> None:
    engine = _engine(url)
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(text(statement))
    finally:
        engine.dispose()


def _scalar(url: str, sql: str) -> Any:  # noqa: ANN401 - a column value
    engine = _engine(url)
    try:
        with engine.begin() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def _columns(url: str) -> set[str]:
    engine = _engine(url)
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    f"WHERE table_name = '{_TABLE}'"
                )
            ).all()
    finally:
        engine.dispose()
    return {str(r[0]) for r in rows}


@pytest.fixture
def as_deployed(database_url: str) -> Iterator[str]:
    """A database at 065 whose table has the shape 065 deployed, holding one fact."""
    _reset_schema(database_url)
    command.upgrade(_alembic_config(database_url), _PRED)
    _execute(
        database_url,
        *(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {name}" for name in _CHECKS),
        *(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}" for column in _NEW),
    )
    engine = _engine(database_url)
    try:
        with engine.begin() as conn:
            row(conn, "users", id="u_a", email="u_a@example.test")
            row(conn, "personas", id="p_a", owner_id="u_a", yaml="x: 1")
            conn.execute(
                text(
                    f"INSERT INTO {_TABLE} (owner_id, persona_id, interaction_id) "
                    "VALUES ('u_a', 'p_a', 'conv_a')"
                )
            )
    finally:
        engine.dispose()
    yield database_url
    _reset_schema(database_url)


def test_a_deployed_fact_gets_one_disclosure_and_no_release(as_deployed: str) -> None:
    assert not set(_NEW) & _columns(as_deployed), "the fixture is the deployed 065 shape"
    command.upgrade(_alembic_config(as_deployed), _HEAD)
    assert set(_NEW) <= _columns(as_deployed)
    got = _scalar(
        as_deployed,
        f"SELECT disclosure_seq = 1 AND released_seq IS NULL AND released_at IS NULL FROM {_TABLE}",
    )
    assert got is True


@pytest.mark.parametrize(
    "values",
    [
        "disclosure_seq = 1, released_seq = 2, released_at = now()",  # ahead of the disclosures
        "released_seq = 1",  # a release with no time
        "released_at = now()",  # a time with no release
    ],
)
def test_the_checks_refuse_an_impossible_release(as_deployed: str, values: str) -> None:
    command.upgrade(_alembic_config(as_deployed), _HEAD)
    with pytest.raises(IntegrityError):
        _execute(as_deployed, f"UPDATE {_TABLE} SET {values}")


def test_a_release_of_the_latest_disclosure_is_accepted(as_deployed: str) -> None:
    command.upgrade(_alembic_config(as_deployed), _HEAD)
    _execute(as_deployed, f"UPDATE {_TABLE} SET released_seq = 1, released_at = now()")
    assert _scalar(as_deployed, f"SELECT released_seq FROM {_TABLE}") == 1


def test_it_is_a_no_op_on_a_fresh_database(database_url: str) -> None:
    """A fresh database already has the columns and checks from the model."""
    _reset_schema(database_url)
    try:
        command.upgrade(_alembic_config(database_url), _HEAD)
        assert set(_NEW) <= _columns(database_url)
        count = _scalar(
            database_url,
            f"SELECT count(*) FROM pg_constraint WHERE conname IN ('{_CHECKS[0]}', '{_CHECKS[1]}')",
        )
        assert count == 2
    finally:
        _reset_schema(database_url)


def test_downgrade_removes_the_release_and_keeps_the_fact(as_deployed: str) -> None:
    cfg = _alembic_config(as_deployed)
    command.upgrade(cfg, _HEAD)
    command.downgrade(cfg, _PRED)
    assert not set(_NEW) & _columns(as_deployed)
    assert _scalar(as_deployed, f"SELECT count(*) FROM {_TABLE}") == 1
    command.upgrade(cfg, _HEAD)
    assert set(_NEW) <= _columns(as_deployed)
