"""Migrations ``057_runs_stop_reason`` and ``058_jobs_live_task_leg_index`` round trip (R9-158).

Both are split-home, so both paths the pattern promises are proven, as in
``test_migration_runs_task_id.py``:

- **Fresh DB:** ``001_initial``'s ``metadata.create_all`` already builds ``runs.stop_reason``,
  ``runs_stop_reason_check`` and ``idx_jobs_live_task_leg``; the guarded statements are
  no-ops and the chain still reaches head.
- **Previously-deployed DB:** the column, CHECK and index are dropped after upgrading to
  the predecessor (a pre-R9-158 schema), then the migrations genuinely add them; the CHECK
  is enforced and the index is the partial expression index the planner can use.
- **Downgrade** to the predecessor removes what they added and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from persona_api.tasks.live_legs import live_task_leg_ids, starting_task_leg_ids
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]  # packages/api
_ALEMBIC_INI = _API_DIR / "alembic.ini"
_PRED = "056_call_end_reason_vocabulary"  # 057's predecessor
_NOW_FOR_PLANS = datetime(2026, 9, 26, tzinfo=UTC)


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


def _execute(database_url: str, sql: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()


def _scalar(database_url: str, sql: str) -> object:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def _state(db: str) -> tuple[bool, bool, str | None]:
    """(stop_reason column present, its CHECK present, the index definition or None)."""
    engine = create_engine(db)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("runs")}
    finally:
        engine.dispose()
    check = _scalar(
        db,
        "SELECT count(*) FROM pg_constraint "
        "WHERE conrelid = 'runs'::regclass AND conname = 'runs_stop_reason_check'",
    )
    index = _scalar(
        db, "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_jobs_live_task_leg'"
    )
    return ("stop_reason" in columns, check == 1, None if index is None else str(index))


def _assert_present(db: str) -> None:
    has_column, has_check, index = _state(db)
    assert (has_column, has_check) == (True, True)
    assert index is not None
    assert "(payload ->> 'task_id'::text)" in index
    assert "type = 'task_leg'::text" in index
    assert all(state in index for state in ("queued", "claimed", "running"))


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    _reset_schema(database_url)
    yield database_url
    _reset_schema(database_url)


# A from-scratch 001->head chain is legitimately longer than the 120s "stuck test" backstop.
@pytest.mark.timeout(300)
def test_fresh_db_reaches_head_with_the_stop_reason_and_the_live_leg_index(clean_db: str) -> None:
    command.upgrade(_alembic_config(clean_db), "head")
    _assert_present(clean_db)


@pytest.mark.timeout(300)
def test_deployed_db_gains_both_and_downgrade_removes_them(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, _PRED)
    _execute(clean_db, "ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_stop_reason_check")
    _execute(clean_db, "ALTER TABLE runs DROP COLUMN IF EXISTS stop_reason")
    _execute(clean_db, "DROP INDEX IF EXISTS idx_jobs_live_task_leg")
    assert _state(clean_db) == (False, False, None)
    _execute(clean_db, "INSERT INTO users (id, email) VALUES ('u', 'u@x')")
    _execute(clean_db, "INSERT INTO personas (id, owner_id, yaml) VALUES ('p', 'u', 'name: x')")
    _execute(
        clean_db,
        "INSERT INTO runs (id, owner_id, persona_id, task, status) "
        "VALUES ('r-old', 'u', 'p', 'g', 'cancelled')",
    )

    # UP: the genuine ADD path.
    command.upgrade(cfg, "head")
    _assert_present(clean_db)
    assert _scalar(clean_db, "SELECT stop_reason FROM runs WHERE id = 'r-old'") is None
    _execute(clean_db, "UPDATE runs SET stop_reason = 'paused' WHERE id = 'r-old'")
    with pytest.raises(IntegrityError):  # the vocabulary is enforced, not decorative
        _execute(clean_db, "UPDATE runs SET stop_reason = 'restart' WHERE id = 'r-old'")

    # DOWN to the predecessor: only what 057 and 058 added is gone.
    command.downgrade(cfg, _PRED)
    assert _state(clean_db) == (False, False, None)
    assert _scalar(clean_db, "SELECT status FROM runs WHERE id = 'r-old'") == "cancelled"


def _prepared(query: object, name: str) -> tuple[str, int]:
    """``query`` compiled for Postgres, its bind parameters renumbered ``$1..$n`` for PREPARE."""
    compiled = query.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    sql = str(compiled)
    for position, param in enumerate(compiled.params, start=1):
        sql = sql.replace(f"%({param})s", f"${position}")
    return (f"PREPARE {name} AS {sql}", len(compiled.params))


@pytest.mark.timeout(300)
def test_the_live_leg_queries_use_the_index_under_a_generic_plan(clean_db: str) -> None:
    # Review MEDIUM-3: the index is only worth having if the queries can use it when a
    # prepared statement is reused for any task id. A generic plan knows no parameter
    # values, so the key, the job type and the states must be literals in the SQL.
    command.upgrade(_alembic_config(clean_db), "head")
    _execute(clean_db, "INSERT INTO users (id, email) VALUES ('u', 'u@x')")
    # Enough live legs across many tasks that one task's legs are a needle: the owner
    # index is useless (one owner), the partial expression index is the selective one.
    _execute(
        clean_db,
        "INSERT INTO jobs (type, owner_id, payload, idempotency_key, state) "
        "SELECT 'task_leg', 'u', jsonb_build_object('task_id', 't' || g), 'k' || g, "
        "(ARRAY['queued', 'claimed', 'running'])[1 + mod(g, 3)] "
        "FROM generate_series(1, 3000) AS g",
    )
    _execute(clean_db, "ANALYZE jobs")
    live_sql, live_n = _prepared(live_task_leg_ids("t1"), "live_legs")
    starting_sql, starting_n = _prepared(
        starting_task_leg_ids("t1", now=_NOW_FOR_PLANS), "starting_legs"
    )
    assert (live_n, starting_n) == (1, 2)
    engine = create_engine(clean_db)
    try:
        with engine.connect() as conn:
            conn.execute(text("SET plan_cache_mode = force_generic_plan"))
            conn.execute(text("SET enable_seqscan = off"))
            conn.execute(text(live_sql))
            conn.execute(text(starting_sql))
            plans = [
                "\n".join(str(row[0]) for row in conn.execute(text(f"EXPLAIN EXECUTE {statement}")))
                for statement in ("live_legs('t7')", "starting_legs('t7', now())")
            ]
    finally:
        engine.dispose()
    for plan in plans:
        assert "idx_jobs_live_task_leg" in plan, plan
