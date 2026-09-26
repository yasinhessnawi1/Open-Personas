"""The live task-leg index is declared once and means the same everywhere (R9-158, 058).

The migration and the canonical model must describe the same index, or a fresh database
(built from the model) and an upgraded one (built by the migration) diverge, and
autogenerate reports drift. The round trip on Postgres is
``tests/integration/test_migration_task_state_truth.py``; this holds the two texts equal
and proves the index is Postgres-only: the community (SQLite) schema never asks for ``->>``,
which older SQLite does not have.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from persona_api.db.community import build_community_metadata
from persona_api.db.models import jobs as jobs_t
from persona_api.tasks.live_legs import live_task_leg_ids, starting_task_leg_ids
from sqlalchemy import Index, create_mock_engine
from sqlalchemy.dialects import postgresql

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "058_jobs_live_task_leg_index.py"
)


def _squash(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _model_index() -> Index:
    return next(ix for ix in jobs_t.indexes if ix.name == "idx_jobs_live_task_leg")


def test_the_model_and_the_migration_declare_the_same_partial_expression_index() -> None:
    index = _model_index()
    expression = _squash(str(next(iter(index.expressions))))
    where = _squash(str(index.dialect_options["postgresql"]["where"]))
    migration = _squash(_MIGRATION.read_text(encoding="utf-8").replace('"\n        "', ""))
    assert (expression, where) == (
        "(payload ->> 'task_id')",
        "type = 'task_leg' AND state IN ('queued', 'claimed', 'running')",
    )
    assert f"ON jobs ({expression}) WHERE {where}" in migration


def test_the_community_schema_asks_nothing_of_sqlite_s_json_operators() -> None:
    # Review MEDIUM-4: ``->>`` exists only from SQLite 3.38, so the index is Postgres-only
    # and a community database on an older SQLite still boots. Read the DDL create_all
    # would emit for SQLite, rather than the SQLite this test happens to run on.
    statements: list[str] = []

    def _capture(sql: object, *_: object, **__: object) -> None:
        statements.append(str(sql.compile(dialect=mock.dialect)))  # type: ignore[attr-defined]

    mock = create_mock_engine("sqlite://", _capture)  # type: ignore[arg-type]
    build_community_metadata().create_all(mock, checkfirst=False)  # type: ignore[arg-type]
    assert any("CREATE TABLE jobs" in s for s in statements)  # the capture saw the schema
    assert [s for s in statements if "->>" in s or "idx_jobs_live_task_leg" in s] == []


def test_the_postgres_schema_still_builds_the_index() -> None:
    statements: list[str] = []

    def _capture(sql: object, *_: object, **__: object) -> None:
        statements.append(str(sql.compile(dialect=mock.dialect)))  # type: ignore[attr-defined]

    mock = create_mock_engine("postgresql+psycopg://", _capture)  # type: ignore[arg-type]
    jobs_t.metadata.create_all(mock, tables=[jobs_t], checkfirst=False)  # type: ignore[arg-type]
    created = [s for s in statements if "idx_jobs_live_task_leg" in s]
    assert len(created) == 1
    assert "((payload ->> 'task_id'))" in created[0]


# --- the queries can use it under a generic plan (review MEDIUM-3) ---------------------


def _postgres_sql(query: object) -> tuple[str, list[str]]:
    compiled = query.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    return (" ".join(str(compiled).split()), sorted(compiled.params))


def test_the_live_leg_queries_spell_the_indexed_expression_and_predicate_as_literals() -> None:
    index = _model_index()
    expression = _squash(str(next(iter(index.expressions)))).replace("payload", "jobs.payload")
    live_sql, live_params = _postgres_sql(live_task_leg_ids("t1"))
    starting_sql, starting_params = _postgres_sql(
        starting_task_leg_ids("t1", now=datetime(2026, 9, 26, tzinfo=UTC))
    )
    for sql in (live_sql, starting_sql):
        assert f"{expression} = %(task_id)s" in sql  # the indexed expression, key literal
        assert "jobs.type = 'task_leg'" in sql  # the index predicate's type, literal
    assert "jobs.state IN ('queued', 'claimed', 'running')" in live_sql
    assert "jobs.state IN ('queued', 'claimed', 'running')" in starting_sql
    # Only the task id (and the clock) stay parameters: nothing the predicate depends on.
    assert (live_params, starting_params) == (["task_id"], ["scheduled_at_1", "task_id"])


def test_the_canonical_index_is_declared_for_postgres_only() -> None:
    statements: list[str] = []

    def _capture(sql: object, *_: object, **__: object) -> None:
        statements.append(str(sql.compile(dialect=mock.dialect)))  # type: ignore[attr-defined]

    mock = create_mock_engine("sqlite://", _capture)  # type: ignore[arg-type]
    jobs_t.metadata.create_all(mock, tables=[jobs_t], checkfirst=False)  # type: ignore[arg-type]
    assert any("CREATE TABLE jobs" in s for s in statements)
    assert [s for s in statements if "idx_jobs_live_task_leg" in s] == []
