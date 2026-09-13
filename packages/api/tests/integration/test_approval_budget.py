"""Per-task budget: effective cap over the ledger + at-most-once extension (Spec A3, T10; A3-D-5).

Against real Postgres (the extension SUM rides ``audit_log``; the extension at-most-once gate is
the ``cas_unpause`` CAS). Concerns:

1. **Effective cap** — contract bound (or platform default for an unconfigured task) + SUM of
   ``budget.extended`` rows; ``check`` classifies OK / APPROACHING (≥80%) / REACHED (≥100%).
2. **Pause-at-cap** — a task at the cap is paused (no new legs) with a ``budget.reached`` account.
3. **One-reply extension is at-most-once** — the extension raises the cap + resumes; a
   **duplicated** extension reply does NOT double-extend (the un-pause CAS).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.tasks import Contract, ContractBounds, Task
from persona_api.approvals import (
    PLATFORM_DEFAULT_BUDGET_MICROS,
    BudgetEnforcer,
    BudgetState,
    parse_extension_micros,
)
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping budget test")
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


def _make_task(
    tasks: TaskStore, *, owner: str, persona: str, task_id: str, cap: int | None, spent: int
) -> Task:
    bounds = ContractBounds(total_budget_micros=cap) if cap is not None else ContractBounds()
    tasks.create(
        Task(
            id=task_id,
            owner_id=owner,
            persona_id=persona,
            contract=Contract(goal="win the appeal", bounds=bounds),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start(owner, task_id, now=_NOW)
    if spent:
        # The ledger accrues via CheckpointStore.append in production; set it directly here.
        with tasks._engine.begin() as conn:  # noqa: SLF001 — test-only ledger seed
            conn.execute(
                text("SELECT set_config('app.current_user_id', :o, true)"),
                {"o": owner},
            )
            conn.execute(
                text("UPDATE tasks SET ledger_model_micros = :s WHERE id = :t"),
                {"s": spent, "t": task_id},
            )
    return tasks.get(owner, task_id)


def _enforcer(engine: Engine, queue: _FakeQueue) -> BudgetEnforcer:
    return BudgetEnforcer(engine=engine, tasks=TaskStore(engine), queue=queue)  # type: ignore[arg-type]


# --- effective cap + state --------------------------------------------------


def test_effective_cap_uses_contract_bound(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    task = _make_task(
        TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1", cap=1000, spent=0
    )
    enforcer = _enforcer(app_engine, _FakeQueue())
    assert enforcer.effective_cap("user_a", task) == 1000


def test_unconfigured_task_uses_platform_default(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    task = _make_task(
        TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1", cap=None, spent=0
    )
    enforcer = _enforcer(app_engine, _FakeQueue())
    # Bounded even with no contract cap (criterion 5).
    assert enforcer.effective_cap("user_a", task) == PLATFORM_DEFAULT_BUDGET_MICROS


def test_check_thresholds(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    enforcer = _enforcer(app_engine, _FakeQueue())
    ok = _make_task(tasks, owner="user_a", persona="persona_a", task_id="t_ok", cap=1000, spent=500)
    near = _make_task(
        tasks, owner="user_a", persona="persona_a", task_id="t_near", cap=1000, spent=850
    )
    over = _make_task(
        tasks, owner="user_a", persona="persona_a", task_id="t_over", cap=1000, spent=1000
    )
    assert enforcer.check("user_a", ok) is BudgetState.OK
    assert enforcer.check("user_a", near) is BudgetState.APPROACHING
    assert enforcer.check("user_a", over) is BudgetState.REACHED


# --- pause-at-cap -----------------------------------------------------------


def test_enforce_pauses_at_cap(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    task = _make_task(
        tasks, owner="user_a", persona="persona_a", task_id="t1", cap=1000, spent=1200
    )
    halt = _enforcer(app_engine, _FakeQueue()).enforce("user_a", task, now=_NOW)
    assert halt is True  # the caller must not enqueue the next leg
    assert tasks.get("user_a", "t1").paused is True  # no new legs run past the cap


def test_enforce_continues_when_ok(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    task = _make_task(tasks, owner="user_a", persona="persona_a", task_id="t1", cap=1000, spent=100)
    assert _enforcer(app_engine, _FakeQueue()).enforce("user_a", task, now=_NOW) is False
    assert tasks.get("user_a", "t1").paused is False


# --- the at-most-once extension ---------------------------------------------


def test_extension_raises_cap_and_resumes(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    queue = _FakeQueue()
    enforcer = _enforcer(app_engine, queue)
    task = _make_task(
        tasks, owner="user_a", persona="persona_a", task_id="t1", cap=1000, spent=1000
    )
    enforcer.enforce("user_a", task, now=_NOW)  # paused at cap

    applied = enforcer.extend("user_a", "t1", 500, now=_NOW)

    assert applied is True
    assert tasks.get("user_a", "t1").paused is False  # resumed
    assert len(queue.enqueued) == 1  # the next leg re-enqueued
    # The effective cap rose by the extension (1000 + 500).
    assert enforcer.effective_cap("user_a", tasks.get("user_a", "t1")) == 1500
    # Spec W1 (D-W1-38, amended): the resumed leg is told the BUDGET was raised. A self
    # scheduled fire had it believe its own schedule woke it, which is a different thing: the
    # leg was cut off mid-work and is being let carry on, not fired afresh by the clock.
    trigger = dict(queue.enqueued[0]["payload"])["trigger"]
    assert trigger["kind"] == "revived"
    assert trigger["reason"] == "the user extended its budget"


def test_duplicated_extension_does_not_double_extend(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    queue = _FakeQueue()
    enforcer = _enforcer(app_engine, queue)
    task = _make_task(
        tasks, owner="user_a", persona="persona_a", task_id="t1", cap=1000, spent=1000
    )
    enforcer.enforce("user_a", task, now=_NOW)

    first = enforcer.extend("user_a", "t1", 500, now=_NOW)
    second = enforcer.extend("user_a", "t1", 500, now=_NOW)  # the duplicated reply

    assert first is True
    assert second is False  # the un-pause CAS rejected the duplicate
    # Exactly one extension applied — the cap rose by 500, not 1000.
    assert enforcer.effective_cap("user_a", tasks.get("user_a", "t1")) == 1500
    assert len(queue.enqueued) == 1  # one resume, not two


# --- the extension-amount parser --------------------------------------------


@pytest.mark.parametrize(
    ("reply", "micros"),
    [
        ("add another 50kr of budget", 500_000),
        ("legg til 50 kr", 500_000),
        ("give it 1500 NOK more", 15_000_000),
        ("just a bit more", None),  # no amount → clarify, never guess
    ],
)
def test_parse_extension_micros(reply: str, micros: int | None) -> None:
    assert parse_extension_micros(reply) == micros
