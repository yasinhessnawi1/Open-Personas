"""Migration ``054_runs_task_id`` round-trip test (Spec W1, T1; D-W1-1 / D-W1-2 / D-W1-25).

Proves the additive migration lands and reverses cleanly on BOTH paths the split-home
pattern promises:

- **Fresh DB:** ``001_initial``'s ``metadata.create_all`` already builds ``runs.task_id``,
  ``fk_runs_task``, ``idx_runs_task``, ``tasks.kind`` and ``tasks_kind_check``; the
  guarded statements are no-ops and the chain still reaches head.
- **Previously-deployed DB:** the columns, FK, index and CHECK are dropped after
  upgrading to the predecessor (reproducing a pre-W1 schema faithfully), then the
  migration genuinely adds them, with the CHECK enforced and the default applied to an
  existing row.
- **Downgrade** to the predecessor removes everything it added and nothing else.

Mirrors the migration-051/039/032 programmatic Alembic pattern (cwd-independent, reset
schema at start AND end). Numbering (D-W1-25): authored as a placeholder off 052 while M5
held 053; renumbered to ``054_runs_task_id`` off M5's 053 at the rebase onto main.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]  # packages/api
_ALEMBIC_INI = _API_DIR / "alembic.ini"
_PRED = "053_auto_topup_notification_kind"  # 054 predecessor (M5 merged first)


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


def _constraints(database_url: str, table: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT conname FROM pg_constraint WHERE conrelid = CAST(:t AS regclass)"),
                {"t": table},
            ).scalars()
            return set(rows)
    finally:
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
            return conn.execute(text(sql)).scalar_one()
    finally:
        engine.dispose()


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    _reset_schema(database_url)
    yield database_url
    _reset_schema(database_url)


def _assert_present(db: str) -> None:
    assert "task_id" in _columns(db, "runs")
    assert "fk_runs_task" in _constraints(db, "runs")
    assert "idx_runs_task" in _indexes(db, "runs")
    assert "kind" in _columns(db, "tasks")
    assert "tasks_kind_check" in _constraints(db, "tasks")


def _assert_absent(db: str) -> None:
    assert "task_id" not in _columns(db, "runs")
    assert "fk_runs_task" not in _constraints(db, "runs")
    assert "idx_runs_task" not in _indexes(db, "runs")
    assert "kind" not in _columns(db, "tasks")
    assert "tasks_kind_check" not in _constraints(db, "tasks")


# A from-scratch 001→head chain is legitimately longer than the 120s "stuck test" backstop.
@pytest.mark.timeout(300)
def test_fresh_db_reaches_head_with_the_run_task_link_and_kind(clean_db: str) -> None:
    command.upgrade(_alembic_config(clean_db), "head")
    _assert_present(clean_db)
    # The pre-W1 columns are untouched (the human label stays; legacy rows read it).
    assert "task" in _columns(clean_db, "runs")


@pytest.mark.timeout(300)
def test_deployed_db_gains_the_columns_and_downgrade_removes_them(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)
    # Reproduce a previously-deployed, pre-W1 schema: at the predecessor, without the
    # columns ``001``'s create_all built from the now-updated canonical metadata.
    command.upgrade(cfg, _PRED)
    _execute(clean_db, "ALTER TABLE runs DROP CONSTRAINT IF EXISTS fk_runs_task")
    _execute(clean_db, "DROP INDEX IF EXISTS idx_runs_task")
    _execute(clean_db, "ALTER TABLE runs DROP COLUMN IF EXISTS task_id")
    _execute(clean_db, "ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_kind_check")
    _execute(clean_db, "ALTER TABLE tasks DROP COLUMN IF EXISTS kind")
    _assert_absent(clean_db)
    # A pre-W1 task row exists before the upgrade: it must read ``standing`` afterwards.
    _execute(clean_db, "INSERT INTO users (id, email) VALUES ('u', 'u@x')")
    _execute(
        clean_db,
        "INSERT INTO personas (id, owner_id, yaml) VALUES ('p', 'u', 'name: x')",
    )
    _execute(
        clean_db,
        "INSERT INTO tasks (id, owner_id, persona_id, contract_json) "
        "VALUES ('t-old', 'u', 'p', '{\"goal\": \"g\"}'::jsonb)",
    )

    # UP: the genuine ADD path.
    command.upgrade(cfg, "head")
    _assert_present(clean_db)
    assert _scalar(clean_db, "SELECT kind FROM tasks WHERE id = 't-old'") == "standing"
    # The CHECK is enforced, not decorative.
    with pytest.raises(IntegrityError):
        _execute(clean_db, "UPDATE tasks SET kind = 'scheduled' WHERE id = 't-old'")
    # The FK is enforced and SET NULL on task delete keeps the run row.
    _execute(
        clean_db,
        "INSERT INTO runs (id, owner_id, persona_id, task, task_id) "
        "VALUES ('r1', 'u', 'p', 'g', 't-old')",
    )
    with pytest.raises(IntegrityError):
        _execute(
            clean_db,
            "INSERT INTO runs (id, owner_id, persona_id, task, task_id) "
            "VALUES ('r2', 'u', 'p', 'g', 'no-such-task')",
        )
    _execute(clean_db, "DELETE FROM tasks WHERE id = 't-old'")
    assert _scalar(clean_db, "SELECT task_id IS NULL FROM runs WHERE id = 'r1'") is True

    # DOWN: only what this migration added is gone (reversible).
    command.downgrade(cfg, _PRED)
    _assert_absent(clean_db)
    assert "task" in _columns(clean_db, "runs")
    assert "state" in _columns(clean_db, "tasks")
