"""The tasks read surface — list / detail / audit (Spec A6, B1).

RLS-scoped reads over the real A2 stores. Drives the route handlers directly (they self-scope every
read, so no middleware GUC is needed): the cross-persona list with state + spend, the detail with
grants + budget + ledger + the terminal report, and the readable audit trail.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from persona.tasks import Contract, Task, TaskCheckpoint, WaitKind
from persona_api.auth import AuthenticatedUser
from persona_api.routes.tasks import (
    cancel_task,
    extend_budget,
    get_task,
    get_task_audit,
    list_tasks,
    pause_task,
    resume_task,
)
from persona_api.schemas.requests import BudgetExtendRequest
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_NOW = datetime(2026, 7, 7, 9, 0, tzinfo=UTC)
_USER = AuthenticatedUser(id="u", email=None)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    from sqlalchemy import create_engine

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping tasks-route test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _request(engine: Engine) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(rls_engine=engine)))


def _seed(su: Engine) -> None:
    with su.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u','u@x')"))
        conn.execute(text("INSERT INTO personas (id, owner_id, yaml) VALUES ('kai','u','name: x')"))


def _task(tasks: TaskStore, task_id: str, goal: str) -> None:
    tasks.create(
        Task(
            id=task_id,
            owner_id="u",
            persona_id="kai",
            contract=Contract(goal=goal),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start("u", task_id, now=_NOW)


async def test_list_tasks_shows_active_and_terminal(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    tasks = TaskStore(app_engine)
    _task(tasks, "t_active", "watch the fares")
    _task(tasks, "t_done", "summarise the newsletters")
    tasks.complete("u", "t_done", now=_NOW)

    summaries = await list_tasks(_request(app_engine), _USER)

    by_id = {s.task_id: s for s in summaries}
    assert by_id["t_active"].status == "just_created"  # started, no checkpoint yet
    assert by_id["t_done"].status == "completed"
    assert by_id["t_done"].budget_cap_micros == 10_000_000  # the platform default cap


async def test_task_detail_renders_contract_grants_budget_and_report(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    tasks = TaskStore(app_engine)
    _task(tasks, "t_done", "file the receipts")
    tasks.complete("u", "t_done", now=_NOW)

    detail = await get_task("t_done", _request(app_engine), _USER)

    assert detail.status == "completed"
    assert detail.goal == "file the receipts"
    grants = {g.category: g.decision for g in detail.grants}
    assert grants["observe"] == "allow"  # free category
    assert grants["spend"] == "gate"  # gated-by-default (default policy)
    assert detail.budget.cap_micros == 10_000_000
    assert detail.budget.state == "ok"
    assert detail.report is not None
    assert detail.report.kind == "completed"


async def test_task_detail_404_for_missing_or_foreign(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    with pytest.raises(HTTPException) as exc:
        await get_task("nope", _request(app_engine), _USER)
    assert exc.value.status_code == 404


async def test_task_audit_trail_reads_the_target_rows(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    _task(TaskStore(app_engine), "t1", "book the dentist")
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (id, user_id, action, target, metadata) "
                "VALUES ('a1','u','budget.extended','t1','{\"amount_micros\": 500}')"
            )
        )
    entries = await get_task_audit("t1", _request(app_engine), _USER)
    # the trail is the task's target rows — lifecycle (task.create/start) + the budget event.
    actions = [e.action for e in entries]
    assert "budget.extended" in actions
    extended = next(e for e in entries if e.action == "budget.extended")
    assert extended.metadata == {"amount_micros": 500}


async def test_list_populates_stuck_cause_only_for_the_stuck_subset(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    tasks = TaskStore(app_engine)
    _task(tasks, "t_active", "watch the fares")  # healthy active — no cause
    # a stuck task: waiting on the user, with a head checkpoint carrying the blocked_on reason.
    _task(tasks, "t_stuck", "book the dentist")
    CheckpointStore(app_engine).append(
        tasks.get("u", "t_stuck"),
        TaskCheckpoint(
            task_id="t_stuck",
            leg_id="leg1",
            checkpoint_seq=0,
            blocked_on="needs your clinic login",
            next_step="share the login",
            updated_at=_NOW,
        ),
        now=_NOW,
    )
    tasks.begin_wait("u", "t_stuck", WaitKind.ON_USER, now=_NOW)

    by_id = {s.task_id: s for s in await list_tasks(_request(app_engine), _USER)}

    assert by_id["t_stuck"].status == "waiting_on_user"
    assert by_id["t_stuck"].stuck_cause == "needs your clinic login"  # loud-with-information
    assert by_id["t_active"].stuck_cause is None  # non-stuck rows carry no cause (additive)


# --- B2: commands (mutations — audited, idempotent, bounded) ---------------------------------


async def test_pause_is_idempotent(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine)
    _task(TaskStore(app_engine), "t1", "watch the fares")
    req = _request(app_engine)

    first = await pause_task("t1", req, _USER)
    assert first.changed is True
    assert first.paused is True
    # a double-click / second tab → a calm no-op, NOT an error.
    second = await pause_task("t1", req, _USER)
    assert second.changed is False
    assert second.paused is True


async def test_resume_reflects_owner_autonomy_pause(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    tasks = TaskStore(app_engine)
    _task(tasks, "t1", "book the dentist")
    tasks.pause("u", "t1", now=_NOW)
    with migrated_engine.begin() as conn:  # the owner's autonomy is paused
        conn.execute(
            text("INSERT INTO owner_autonomy_pause (owner_id, actor) VALUES ('u','user_via_ui')")
        )

    result = await resume_task("t1", _request(app_engine), _USER)

    assert result.changed is True  # the overlay is cleared
    assert result.owner_autonomy_paused is True  # but it won't run — reflected, not silently armed


async def test_cancel_is_honest_and_terminal_is_a_noop(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    _task(TaskStore(app_engine), "t1", "reply to the landlord")
    req = _request(app_engine)

    cancelled = await cancel_task("t1", req, _USER)
    assert cancelled.changed is True
    assert cancelled.status == "cancelled"
    assert cancelled.note  # states the in-flight fate, not implied
    # cancelling an already-terminal task → a calm no-op on the durable truth.
    again = await cancel_task("t1", req, _USER)
    assert again.changed is False
    assert again.status == "cancelled"


async def test_budget_extend_is_bounded_and_at_most_once(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine)
    tasks = TaskStore(app_engine)
    _task(tasks, "t1", "renew the domain")
    tasks.pause("u", "t1", now=_NOW)  # budget-paused (the extend precondition)
    req = _request(app_engine)

    # bounded: an over-the-max increment is refused, not written.
    with pytest.raises(HTTPException) as exc:
        await extend_budget("t1", BudgetExtendRequest(amount_micros=10_000_001), req, _USER)
    assert exc.value.status_code == 422

    result = await extend_budget("t1", BudgetExtendRequest(amount_micros=500_000), req, _USER)
    assert result.applied is True
    assert result.new_cap_micros == result.old_cap_micros + 500_000  # old → new, bounded
    # the task un-paused (the extend armed it); a second extend now no-ops (at-most-once).
    again = await extend_budget("t1", BudgetExtendRequest(amount_micros=500_000), req, _USER)
    assert again.applied is False
