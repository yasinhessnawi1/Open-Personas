"""Kill switches: the reason-scoped runnable invariant + cancel/suspend/global-pause (Spec A3, T11).

Against real Postgres (the RLS persona-suspend + the operational global flag). The load-bearing
property is the **reason-scoped invariant**: independent pause sources, so clearing one (budget's
``cas_unpause``) never resumes a task another source holds.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.tasks import Contract, Task
from persona_api.approvals import (
    KillSwitchCommand,
    KillSwitchStore,
    parse_kill_switch,
)
from persona_api.approvals.budget import BudgetEnforcer
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping kill-switch test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    def enqueue(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


def _seed(engine: Engine, user: str, persona: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": user, "e": f"{user}@example.com"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
            {"p": persona, "o": user},
        )


def _active_task(tasks: TaskStore, *, owner: str, persona: str, task_id: str) -> Task:
    tasks.create(
        Task(
            id=task_id,
            owner_id=owner,
            persona_id=persona,
            contract=Contract(goal="win the appeal"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    return tasks.start(owner, task_id, now=_NOW)


def _kill_switch(engine: Engine) -> KillSwitchStore:
    tasks = TaskStore(engine)
    continuation = TaskContinuation(
        task_store=tasks, queue=_FakeQueue(), checkpoint_store=CheckpointStore(engine)
    )  # type: ignore[arg-type]
    return KillSwitchStore(engine, continuation=continuation)


# --- the reason-scoped runnable invariant (the non-negotiable) ---------------


def test_clearing_budget_does_not_resume_a_suspended_persona(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    task = _active_task(tasks, owner="user_a", persona="persona_a", task_id="t1")
    ks = _kill_switch(app_engine)
    budget = BudgetEnforcer(engine=app_engine, tasks=tasks, queue=_FakeQueue())  # type: ignore[arg-type]

    # Two independent sources co-occur: budget pauses the task AND the persona is suspended.
    tasks.pause("user_a", "t1", now=_NOW)  # budget overlay
    ks.suspend_persona("user_a", "persona_a", now=_NOW)
    assert ks.is_runnable("user_a", tasks.get("user_a", "t1")) is False

    # A budget extension clears the budget overlay (cas_unpause) — but the persona is STILL
    # suspended, so the task must NOT become runnable. The invariant.
    assert budget.extend("user_a", "t1", 500, now=_NOW) is True  # un-paused the budget overlay
    task = tasks.get("user_a", "t1")
    assert task.paused is False  # budget source cleared
    assert ks.is_runnable("user_a", task) is False  # persona-suspend still holds


def test_runnable_only_when_no_source_holds(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    _active_task(tasks, owner="user_a", persona="persona_a", task_id="t1")
    ks = _kill_switch(app_engine)
    assert ks.is_runnable("user_a", tasks.get("user_a", "t1")) is True
    ks.global_pause(actor="operator_1", now=_NOW)
    assert ks.is_runnable("user_a", tasks.get("user_a", "t1")) is False  # global holds
    ks.global_resume(actor="operator_1", now=_NOW)
    assert ks.is_runnable("user_a", tasks.get("user_a", "t1")) is True


# --- task-cancel: terminal, cannot be revived -------------------------------


def test_cancel_is_terminal_and_extension_cannot_revive(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    _active_task(tasks, owner="user_a", persona="persona_a", task_id="t1")
    ks = _kill_switch(app_engine)
    budget = BudgetEnforcer(engine=app_engine, tasks=tasks, queue=_FakeQueue())  # type: ignore[arg-type]

    ks.cancel_task("user_a", "t1", now=_NOW)
    cancelled = tasks.get("user_a", "t1")
    assert cancelled.state.value == "cancelled"
    assert ks.is_runnable("user_a", cancelled) is False  # terminal → never runnable
    # A stale extension reply cannot bring it back to life (cas_unpause needs paused=true;
    # cancel cleared the overlay, and the task is terminal regardless).
    assert budget.extend("user_a", "t1", 500, now=_NOW) is False


# --- persona-suspend (resumable, RLS-scoped) --------------------------------


def test_persona_suspend_and_resume(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    ks = _kill_switch(app_engine)
    assert ks.is_persona_suspended("user_a", "persona_a") is False
    ks.suspend_persona("user_a", "persona_a", now=_NOW)
    ks.suspend_persona("user_a", "persona_a", now=_NOW)  # idempotent
    assert ks.is_persona_suspended("user_a", "persona_a") is True
    ks.resume_persona("user_a", "persona_a", now=_NOW)
    assert ks.is_persona_suspended("user_a", "persona_a") is False


def test_persona_suspend_is_owner_scoped(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    _seed(migrated_engine, "user_b", "persona_b")
    ks = _kill_switch(app_engine)
    ks.suspend_persona("user_a", "persona_a", now=_NOW)
    # user_b cannot see user_a's suspension (RLS).
    assert ks.is_persona_suspended("user_b", "persona_a") is False


# --- per-owner autonomy pause (A6-D-8; owner-scoped, RLS; the completeness teeth) -----------


def test_owner_pause_and_resume_presence_based_and_audited(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    ks = _kill_switch(app_engine)
    assert ks.is_owner_autonomy_paused("user_a") is False
    ks.pause_owner("user_a", actor="user_via_ui", now=_NOW)
    ks.pause_owner("user_a", actor="user_via_ui", now=_NOW)  # idempotent (presence-based)
    assert ks.is_owner_autonomy_paused("user_a") is True
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT user_id FROM audit_log WHERE action = 'autonomy.owner_pause' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        ).first()
    assert row is not None
    assert row.user_id == "user_a"
    ks.resume_owner("user_a", now=_NOW)  # presence-based → row deleted
    assert ks.is_owner_autonomy_paused("user_a") is False


def test_owner_pause_makes_task_non_runnable(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    _active_task(tasks, owner="user_a", persona="persona_a", task_id="t1")
    ks = _kill_switch(app_engine)
    assert ks.is_runnable("user_a", tasks.get("user_a", "t1")) is True
    ks.pause_owner("user_a", actor="user_via_ui", now=_NOW)  # the task-leg origination gate
    assert ks.is_runnable("user_a", tasks.get("user_a", "t1")) is False
    ks.resume_owner("user_a", now=_NOW)
    assert ks.is_runnable("user_a", tasks.get("user_a", "t1")) is True


def test_owner_pause_is_owner_scoped(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    _seed(migrated_engine, "user_b", "persona_b")
    ks = _kill_switch(app_engine)
    ks.pause_owner("user_a", actor="user_via_ui", now=_NOW)
    # user_b is NOT paused by user_a's pause (RLS isolation; owner-level, but per-owner).
    assert ks.is_owner_autonomy_paused("user_b") is False


def test_owner_pause_self_scopes_past_a_foreign_guc(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The completeness teeth (A6-D-8): a paused owner reads True even when the caller's ambient
    RLS context is ANOTHER owner's — because the predicate self-scopes. A naive ambient-GUC read
    fails CLOSED and would leak origination past the paused owner. This test proves both the
    hazard (the naive read is hidden) and that the predicate defeats it.
    """
    _seed(migrated_engine, "user_a", "persona_a")
    _seed(migrated_engine, "user_b", "persona_b")
    ks = _kill_switch(app_engine)
    ks.pause_owner("user_a", actor="user_via_ui", now=_NOW)

    # The hazard is real: under user_b's RLS context, a naive read of user_a's row is HIDDEN
    # (RLS fails closed). A background gate that trusted the ambient GUC would see "not paused".
    with app_engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_user_id', 'user_b', true)"))
        naive = conn.execute(
            text("SELECT owner_id FROM owner_autonomy_pause WHERE owner_id = 'user_a'")
        ).first()
    assert naive is None

    # The predicate SELF-SCOPES → still True for user_a regardless of any ambient context,
    # and correctly False for user_b (RLS isolation holds the other direction too).
    assert ks.is_owner_autonomy_paused("user_a") is True
    assert ks.is_owner_autonomy_paused("user_b") is False


# --- global-pause (operational, audited) ------------------------------------


def test_global_pause_resume_is_audited_with_actor(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    ks = _kill_switch(app_engine)
    assert ks.is_globally_paused() is False
    ks.global_pause(actor="operator_1", now=_NOW)
    assert ks.is_globally_paused() is True
    # Audited with the operator actor (the API route authorises operator-only).
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT user_id FROM audit_log WHERE action = 'autonomy.global_pause' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        ).first()
    assert row is not None
    assert row.user_id == "operator_1"
    ks.global_resume(actor="operator_1", now=_NOW)
    assert ks.is_globally_paused() is False


# --- the conversation parse (NO/EN) -----------------------------------------


@pytest.mark.parametrize(
    ("reply", "command"),
    [
        ("cancel this task", KillSwitchCommand.CANCEL_TASK),
        ("avbryt denne oppgaven", KillSwitchCommand.CANCEL_TASK),
        ("suspend this persona", KillSwitchCommand.SUSPEND_PERSONA),
        ("stopp denne personaen", KillSwitchCommand.SUSPEND_PERSONA),
        ("pause everything now", KillSwitchCommand.GLOBAL_PAUSE),
        ("stopp all autonomi", KillSwitchCommand.GLOBAL_PAUSE),
        ("resume all autonomy", KillSwitchCommand.GLOBAL_RESUME),
        ("what's the weather", None),
    ],
)
def test_parse_kill_switch(reply: str, command: KillSwitchCommand | None) -> None:
    assert parse_kill_switch(reply) == command
