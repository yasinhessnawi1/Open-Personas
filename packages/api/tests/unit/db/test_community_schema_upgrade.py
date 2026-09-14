"""A community database that predates a column gains it at boot (R9-174).

The regression this file exists for: ``create_community_schema`` was
``metadata.create_all(engine)``, and ``create_all`` defaults to
``checkfirst=True``, so it created missing TABLES and silently skipped every
table that already existed. It never altered one. Community deliberately bypasses
the cloud Alembic chain (Postgres-only DDL), so an existing database never gained
a column: upgrading across a release that added one crashed at boot with a raw
``sqlite3.OperationalError: no such column``, thrown from the startup restart
sweep. It shipped three times (migrations 047, 052, 054) because every gate built
a FRESH database, where ``create_all`` is correct and the bug cannot appear.

So these tests do the one thing those gates never did: they build a database,
make it OLD, and boot the real app on it. ``runs.task_id`` is the worked case
because it is the column that actually broke, and the restart sweep reads it on
every boot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.errors import CommunitySchemaUpgradeError

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine

# The ``runs`` table as a community database built before Spec W1's migration 054
# carried it: no ``task_id`` column, and so none of the constraints or indexes that
# name it either. Everything else is today's shape, which is exactly the drift a
# real upgrade meets.
_RUNS_BEFORE_TASK_ID = """
CREATE TABLE runs (
    id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT DEFAULT 'running' NOT NULL,
    steps JSON DEFAULT '[]' NOT NULL,
    output TEXT,
    error TEXT,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
    finished_at DATETIME,
    PRIMARY KEY (id)
)
"""

# The same table missing ``started_at`` as well. ``started_at`` stands in for a
# release that adds a NOT NULL column defaulting to ``CURRENT_TIMESTAMP``, a shape
# SQLite refuses to ADD because the default is not a literal. ``task_id`` is left
# out too, so the refusal has a perfectly addable column sitting next to the
# unaddable one: that is what proves nothing was written before the refusal.
_RUNS_MISSING_AN_UNADDABLE_COLUMN = """
CREATE TABLE runs (
    id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    persona_id TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT DEFAULT 'running' NOT NULL,
    steps JSON DEFAULT '[]' NOT NULL,
    output TEXT,
    error TEXT,
    finished_at DATETIME,
    PRIMARY KEY (id)
)
"""


def _column_names(engine: Engine, table: str) -> list[str]:
    """What SQLite actually has for ``table`` right now."""
    with engine.begin() as conn:
        return [str(row[1]) for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")]


def _age_the_runs_table(engine: Engine, ddl: str) -> None:
    """Replace ``runs`` with an older shape, as an installed database would carry it.

    A rebuild rather than ``ALTER TABLE ... DROP COLUMN`` because SQLite refuses to
    drop a column named in a foreign key, which ``runs.task_id`` is. Nothing
    references ``runs``, so dropping it is safe.
    """
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE runs")
        conn.exec_driver_sql(ddl)


def _community_app(tmp_path: Path, embedder: Embedder, monkeypatch: pytest.MonkeyPatch) -> object:
    """The real community app, on the SQLite file in ``tmp_path``."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    from persona_api.services import persona_service

    monkeypatch.setattr(persona_service, "default_embedder", lambda *_a, **_k: embedder)
    config = APIConfig(
        edition=Edition.community,
        community_db_path=tmp_path / "community.db",
        community_memory_path=tmp_path / "chroma",
        workspace_root=tmp_path / "work",
        audit_root=str(tmp_path / "audit"),
    )
    return create_app(config)


def test_boot_adds_the_column_an_older_community_database_never_gained(
    tmp_path: Path, embedder: Embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade path: an old database boots, gains ``runs.task_id``, keeps its rows."""
    db_path = tmp_path / "community.db"
    engine = make_community_engine(db_path)
    create_community_schema(engine)
    _age_the_runs_table(engine, _RUNS_BEFORE_TASK_ID)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO runs (id, owner_id, persona_id, task, status) "
            "VALUES ('run-from-before', 'local-owner', 'p1', 'an older run', 'completed')"
        )
    engine.dispose()

    # The setup must actually have removed the column, or this test verifies nothing.
    assert "task_id" not in _column_names(make_community_engine(db_path), "runs")

    app = _community_app(tmp_path, embedder, monkeypatch)
    with TestClient(app) as client:  # the lifespan: schema reconcile, then the restart sweep
        # The app is genuinely serving, not merely constructed.
        assert client.get("/v1/personas").status_code == 200
        booted_engine = client.app.state.rls_engine
        assert "task_id" in _column_names(booted_engine, "runs")
        # ADD COLUMN, not a rebuild: the row that was there is still there.
        with booted_engine.begin() as conn:
            rows = list(conn.exec_driver_sql("SELECT id, task_id FROM runs"))
        assert rows == [("run-from-before", None)]


def test_boot_refuses_a_column_sqlite_cannot_add_and_says_what_to_do(
    tmp_path: Path, embedder: Embedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half: what cannot be reconciled fails loudly, not three layers down."""
    db_path = tmp_path / "community.db"
    engine = make_community_engine(db_path)
    create_community_schema(engine)
    _age_the_runs_table(engine, _RUNS_MISSING_AN_UNADDABLE_COLUMN)
    engine.dispose()

    assert "started_at" not in _column_names(make_community_engine(db_path), "runs")

    app = _community_app(tmp_path, embedder, monkeypatch)
    with pytest.raises(CommunitySchemaUpgradeError) as caught, TestClient(app):
        pass  # pragma: no cover - the lifespan raises before the body runs

    message = str(caught.value)
    # Names the table and the column, so the operator knows what is wrong.
    assert "runs.started_at" in message
    # Says what to do about it, in both directions.
    assert "PERSONA_COMMUNITY_DB_MODE=auto" in message
    assert str(db_path) in message
    assert caught.value.context["reason"] == "non_literal_default"
    assert caught.value.context["table"] == "runs"
    assert caught.value.context["column"] == "started_at"
    # It refused before writing anything, which is what the message promises:
    # ``task_id`` was missing too and is perfectly addable, and it was not added.
    assert "task_id" not in _column_names(make_community_engine(db_path), "runs")
