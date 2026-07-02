"""Cross-channel steering is owner-scoped (Spec A4, T12; criterion 9).

"Steerable from another channel" must never mean "steerable across tenants." The steering
mutation goes through the owner-scoped ``TaskStore`` with the caller's ``owner_id`` (the worker
injects it from its handle), so on the real stack (real Postgres, the non-superuser ``persona_app``
role, real RLS): owner A can pause/cancel A's own task from any channel; A **cannot** touch B's
task (a cross-tenant steer is a not-found, never a silent cross-owner mutation); and B's task
genuinely exists and is steerable by B (the non-vacuity control that rules out an empty-table
false pass).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.errors import TaskNotFoundError
from persona.tasks import Contract, Task, TaskState
from persona_api.services.task_steering_service import TaskSteeringService
from persona_api.tasks import TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ordering: migrations first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_tenants(engine: Engine) -> None:
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


def _active_task(task_id: str, owner_id: str, persona_id: str) -> Task:
    return Task(
        id=task_id,
        owner_id=owner_id,
        persona_id=persona_id,
        contract=Contract(goal="a standing task"),
        state=TaskState.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.asyncio
async def test_cross_channel_steer_is_owner_scoped_and_non_vacuous(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    _seed_tenants(migrated_engine)
    tasks = TaskStore(app_engine)
    tasks.create(_active_task("t_a", "user_a", "persona_a"))
    tasks.create(_active_task("t_b", "user_b", "persona_b"))
    steering = TaskSteeringService(tasks=tasks)

    # Owner A steers A's own task (channel-agnostic — the verb + task id are all it takes).
    await steering.steer({"owner_id": "user_a", "task_id": "t_a", "verb": "cancel"})
    assert tasks.get("user_a", "t_a").state is TaskState.CANCELLED

    # Non-vacuity: B's task exists and IS steerable by B.
    await steering.steer({"owner_id": "user_b", "task_id": "t_b", "verb": "pause"})
    assert tasks.get("user_b", "t_b").paused is True

    # Cross-tenant: A cannot steer B's task. Under RLS the task is not-found for A, so the cancel
    # is a benign no-op for A (never a "still running" failure for a task A can't see) — and
    # crucially it never mutates B's row. The load-bearing guarantee is the untouched row.
    await steering.steer({"owner_id": "user_a", "task_id": "t_b", "verb": "cancel"})
    assert tasks.get("user_b", "t_b").state is not TaskState.CANCELLED
    # A truly cannot see B's task (the not-found above was A-scoped, not an empty table).
    with pytest.raises(TaskNotFoundError):
        tasks.get("user_a", "t_b")  # A truly cannot see it — proves the scoping is real
