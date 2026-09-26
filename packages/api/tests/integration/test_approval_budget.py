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
from persona.tasks import Contract, ContractBounds, ScheduledFire, Task
from persona_api.approvals import (
    PLATFORM_DEFAULT_BUDGET_MICROS,
    BudgetEnforcer,
    BudgetState,
    parse_extension_micros,
)
from persona_api.approvals.budget import ExtensionOutcome
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
_FIRE = ScheduledFire(schedule_id="sched-1", fire_time=_NOW)


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

    def count_spent_attempts(self, *, owner_id: str, idempotency_key: str) -> int:  # noqa: ARG002
        return 0  # R9-158: the extension resumes through the continuation's seam


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

    applied = enforcer.extend("user_a", "t1", 500, now=_NOW).outcome

    assert applied is ExtensionOutcome.APPLIED
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

    first = enforcer.extend("user_a", "t1", 500, now=_NOW).outcome
    second = enforcer.extend("user_a", "t1", 500, now=_NOW).outcome  # the duplicated reply

    assert first is ExtensionOutcome.APPLIED
    # R9-158: the gate is the cap, under a row lock. The first extension lifted the task off
    # its cap, so the duplicate finds it no longer at the cap.
    assert second is ExtensionOutcome.NOT_AT_CAP
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


# --- the cap's unit, end to end (R9-161) ------------------------------------


#: One real leg: an OpenRouter response-side actual of 0.04 USD = 4¢ = 400 ledger micros,
#: over a run of 9 000 tokens. The two numbers are 22x apart, which is the size of the lie
#: the stand-in meter told when it accrued ``sum(step.tokens)`` into this ledger.
_LEG_COST_USD = 0.04
_LEG_COST_MICROS = 400
_LEG_TOKENS = 9_000


class _PricedRunner:
    """A finished leg reporting one real, priceable model call (no model)."""

    async def run(self, task, *, on_event, cancel_token, on_step_usage=None):  # noqa: ANN001, ANN202, ARG002
        from persona_runtime.agentic.events import RunEvent
        from persona_runtime.agentic.run import Run, RunStatus, StepUsage
        from persona_runtime.agentic.step import Step, StepType

        await on_event(RunEvent.thinking(0))
        if on_step_usage is not None:
            await on_step_usage(
                StepUsage(
                    step=0,
                    provider="openrouter",
                    model="z-ai/glm-4.6",
                    prompt_tokens=8_000,
                    completion_tokens=1_000,
                    cost_usd=_LEG_COST_USD,
                )
            )
        return Run(
            persona_id="persona_a",
            task=task,
            status=RunStatus.COMPLETED,
            steps=[Step(type=StepType.FINAL, content="done", tokens=_LEG_TOKENS)],
            output="done",
            started_at=_NOW,
            finished_at=_NOW,
        )


class _Builder:
    def build(self, task_id: str, persona_id: str, box, *, task: object = None) -> _PricedRunner:  # noqa: ANN001, ARG002
        return _PricedRunner()


class _Ctx:
    def __init__(self, owner: str) -> None:
        self._owner = owner
        self.meter_calls: list[int] = []

    @property
    def owner_id(self) -> str:
        return self._owner

    @property
    def job_id(self) -> str:
        return "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail=None) -> None:  # noqa: ANN001, ARG002
        self.meter_calls.append(amount_micros)


async def _run_one_leg(app_engine: Engine, task_id: str) -> None:
    """Drive the REAL leg handler, so the ledger is filled by the production meter."""
    from persona_api.tasks import CheckpointStore, TaskLegHandler, TaskLegPayload

    handler = TaskLegHandler(
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_Builder(),  # type: ignore[arg-type]
    )
    await handler.handle(
        TaskLegPayload(task_id=task_id, predecessor_seq=None, trigger=_FIRE), _Ctx("user_a")
    )


@pytest.mark.asyncio
async def test_a_cap_set_in_currency_is_enforced_in_currency(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """R9-161, the whole chain: contract bound → the leg's metered spend → the ledger →
    ``BudgetEnforcer.check``'s verdict, through the real handler, the real store CAS and the
    real enforcer against real Postgres.

    Two identical legs; the only difference is the number in the contract. A cap set to what
    the leg actually costs is REACHED. A cap set to the leg's TOKEN count, the number the
    ledger used to accrue and 22x the real cost here, is not even close.

    Before the fix the ledger held 9 000, so the token-sized cap tripped exactly and the
    money cap tripped too, for a reason that had nothing to do with money: any cap below the
    token count tripped and any cap above it survived, whatever currency the user thought
    they were naming. The token-sized cap is therefore the assertion that carries the fix:
    it is REACHED before and OK after, and nothing in the suite used to say so.
    """
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    _make_task(
        tasks, owner="user_a", persona="persona_a", task_id="t_money", cap=_LEG_COST_MICROS, spent=0
    )
    _make_task(
        tasks, owner="user_a", persona="persona_a", task_id="t_tokens", cap=_LEG_TOKENS, spent=0
    )

    await _run_one_leg(app_engine, "t_money")
    await _run_one_leg(app_engine, "t_tokens")

    enforcer = _enforcer(app_engine, _FakeQueue())
    money = tasks.get("user_a", "t_money")
    tokens = tasks.get("user_a", "t_tokens")

    # The ledger carries the leg's money (400), not its tokens (9 000).
    assert money.ledger.total_micros == _LEG_COST_MICROS
    # ...and that is what the cap is compared against.
    assert enforcer.check("user_a", money) is BudgetState.REACHED
    assert enforcer.check("user_a", tokens) is BudgetState.OK


@pytest.mark.asyncio
async def test_the_cap_pauses_the_task_at_its_real_cost(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """And the verdict has teeth: a task that has spent its cap is paused, so no further
    leg runs until the user extends. This is the consequence the currency unit buys: a
    money cap that actually stops the work at the amount the user named."""
    _seed(migrated_engine, "user_a", "persona_a")
    tasks = TaskStore(app_engine)
    _make_task(
        tasks, owner="user_a", persona="persona_a", task_id="t1", cap=_LEG_COST_MICROS, spent=0
    )

    await _run_one_leg(app_engine, "t1")

    enforcer = _enforcer(app_engine, _FakeQueue())
    halt = enforcer.enforce("user_a", tasks.get("user_a", "t1"), now=_NOW)

    assert halt is True
    assert tasks.get("user_a", "t1").paused is True
