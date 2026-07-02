"""Cross-tenant RLS denial for the A4 task-state reader (Spec A4, T7; A4-D-5).

The introspection tool's reader must be owner-scoped and fail-closed. This proves it on the real
stack (real Postgres, the non-superuser ``persona_app`` role, real RLS): B's task EXISTS under B
(the non-vacuity control that rules out an empty-table false pass), and A's reader cannot see it —
``get_task`` is a not-found and ``list_active`` omits it. The reader is a thin owner-bound wrapper
over the RLS-scoped stores, so this is the same RLS guarantee A2's stores carry, asserted at the
introspection surface.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.errors import TaskNotFoundError
from persona.tasks import Contract, Task
from persona_api.tasks import CheckpointStore, TaskStore
from persona_api.tasks.reader import APITaskStateReader
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ordering: ensures migrations ran
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_tenants(engine: Engine) -> None:
    """Seed the FK parents (users + personas) for two tenants via the privileged engine."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email) VALUES "
                "('user_a','a@example.com'),('user_b','b@example.com') ON CONFLICT DO NOTHING"
            )
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES "
                "('persona_a','user_a','name: a'),('persona_b','user_b','name: b') "
                "ON CONFLICT DO NOTHING"
            )
        )


def _task(task_id: str, owner_id: str, persona_id: str, goal: str) -> Task:
    return Task(
        id=task_id,
        owner_id=owner_id,
        persona_id=persona_id,
        contract=Contract(goal=goal),
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_reader_is_owner_scoped_and_cross_tenant_denial_is_non_vacuous(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    _seed_tenants(migrated_engine)
    tasks = TaskStore(app_engine)
    tasks.create(_task("t_a", "user_a", "persona_a", "A's standing task"))
    tasks.create(_task("t_b", "user_b", "persona_b", "B's standing task"))

    reader_a = APITaskStateReader(TaskStore(app_engine), CheckpointStore(app_engine), "user_a")
    reader_b = APITaskStateReader(TaskStore(app_engine), CheckpointStore(app_engine), "user_b")

    # Non-vacuity: B's task genuinely EXISTS, visible to B (rules out an empty-table false pass).
    assert reader_b.get_task("t_b").contract.goal == "B's standing task"
    assert [t.id for t in reader_b.list_active()] == ["t_b"]

    # Cross-tenant denial: A cannot see B's task — not by get_task, not in the list.
    with pytest.raises(TaskNotFoundError):
        reader_a.get_task("t_b")
    a_ids = {t.id for t in reader_a.list_active()}
    assert "t_b" not in a_ids
    assert a_ids == {"t_a"}
