"""The task-leg job handler — A2-R-4 end-to-end at the job/re-delivery layer (Spec A2, T7).

Runs the handler against the REAL ``TaskStore`` + ``CheckpointStore`` (real Postgres,
``alembic upgrade head``) with a fake runner (no model). The proof: forcing a re-delivery
(calling ``handle`` twice with the SAME payload) is a clean no-op via the store CAS — one
checkpoint, head advanced once, ledger accrued once — while A0 meters each execution. The
handler adds NO second idempotency check; the no-op derives from the store CAS alone.
"""

# ruff: noqa: ARG001, ARG002, ANN001 — fixture-ordering + Protocol-conformance test stubs.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.tasks import (
    Contract,
    ScheduledFire,
    Task,
    TaskState,
)
from persona_api.jobs.queue import JobQueue
from persona_api.tasks import (
    CheckpointStore,
    TaskContinuation,
    TaskLegHandler,
    TaskLegPayload,
    TaskStore,
)
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import CancelToken, Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)
_TRIGGER = ScheduledFire(schedule_id="sched-1", fire_time=_NOW)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


class _FakeRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, task, *, on_event, cancel_token: CancelToken) -> Run:
        self.calls += 1
        await on_event(RunEvent.thinking(0))
        return Run(
            persona_id="persona_a",
            task=task,
            status=RunStatus.COMPLETED,
            steps=[Step(type=StepType.FINAL, content="found 1620kr", tokens=100)],
            output="found 1620kr (SAS, Tue)",
            started_at=_NOW,
            finished_at=_NOW,
        )


class _FakeRunnerBuilder:
    def __init__(self, runner: _FakeRunner) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box) -> _FakeRunner:
        return self._runner


class _FakeContext:
    """A minimal JobContext: owner + a recording meter (what the handler uses)."""

    def __init__(self, owner_id: str) -> None:
        self._owner_id = owner_id
        self.meter_calls: list[int] = []

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def job_id(self) -> str:
        return "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail=None) -> None:
        self.meter_calls.append(amount_micros)


def _seed_active_task(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('user_a','a@example.com')"))
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES ('persona_a','user_a','name: x')"
            )
        )
    # Create the task via the store, then start it (so a leg can append).
    tasks = TaskStore(engine)
    task = Task(
        id="t1",
        owner_id="user_a",
        persona_id="persona_a",
        contract=Contract(goal="find the cheapest fare"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    tasks.create(task)
    tasks.start("user_a", "t1", now=_NOW)


@pytest.mark.asyncio
async def test_leg_runs_and_writes_checkpoint(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_active_task(migrated_engine)
    runner = _FakeRunner()
    handler = TaskLegHandler(
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_FakeRunnerBuilder(runner),
    )
    ctx = _FakeContext("user_a")
    payload = TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER)
    await handler.handle(payload, ctx)

    checkpoints = CheckpointStore(app_engine)
    latest = checkpoints.get_latest("user_a", "t1")
    assert latest is not None
    assert latest.checkpoint_seq == 0
    task = TaskStore(app_engine).get("user_a", "t1")
    assert task.head_checkpoint_seq == 0
    assert task.ledger.model_micros == 100  # one step × 100 tokens
    assert ctx.meter_calls == [100]


@pytest.mark.asyncio
async def test_redelivery_is_idempotent_via_store_cas(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_active_task(migrated_engine)
    runner = _FakeRunner()
    handler = TaskLegHandler(
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_FakeRunnerBuilder(runner),
    )
    ctx = _FakeContext("user_a")
    payload = TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER)

    await handler.handle(payload, ctx)  # first delivery
    await handler.handle(payload, ctx)  # forced re-delivery (same payload)

    tasks = TaskStore(app_engine)
    checkpoints = CheckpointStore(app_engine)
    task = tasks.get("user_a", "t1")
    # Idempotent: head advanced once, ledger accrued once, one checkpoint.
    assert task.head_checkpoint_seq == 0
    assert task.ledger.model_micros == 100  # NOT 200
    assert len(checkpoints.list_recent("user_a", "t1", limit=10)) == 1
    # The leg re-ran (at-least-once) and A0 metered BOTH executions (forensics)...
    assert runner.calls == 2
    assert ctx.meter_calls == [100, 100]
    # ...but the task ledger accrued exactly once (the CAS).


@pytest.mark.asyncio
async def test_second_leg_progresses(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_active_task(migrated_engine)
    runner = _FakeRunner()
    handler = TaskLegHandler(
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_FakeRunnerBuilder(runner),
    )
    ctx = _FakeContext("user_a")
    await handler.handle(TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER), ctx)
    # A genuine next leg carries predecessor_seq=0 → writes seq 1.
    await handler.handle(TaskLegPayload(task_id="t1", predecessor_seq=0, trigger=_TRIGGER), ctx)
    task = TaskStore(app_engine).get("user_a", "t1")
    assert task.head_checkpoint_seq == 1
    assert task.ledger.model_micros == 200  # two distinct legs
    assert len(CheckpointStore(app_engine).list_recent("user_a", "t1", limit=10)) == 2


@pytest.mark.asyncio
async def test_handler_completes_task_via_continuation(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_active_task(migrated_engine)
    handler = TaskLegHandler(
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_FakeRunnerBuilder(_FakeRunner()),
        continuation=TaskContinuation(task_store=TaskStore(app_engine), queue=JobQueue(app_engine)),
    )
    await handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER), _FakeContext("user_a")
    )
    # The COMPLETED leg drove the task terminal via the continuation.
    assert TaskStore(app_engine).get("user_a", "t1").state == TaskState.COMPLETED


class _ContinueRunner:
    """A leg that hits its step budget → a CONTINUE outcome (another leg would follow)."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, task, *, on_event, cancel_token: CancelToken) -> Run:
        self.calls += 1
        return Run(
            persona_id="persona_a",
            task=task,
            status=RunStatus.MAX_STEPS_REACHED,  # not FINAL → CONTINUE
            steps=[Step(type=StepType.REASONING, content="still working", tokens=100)],
            output=None,
            started_at=_NOW,
            finished_at=_NOW,
        )


def _leg_job_count(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM jobs WHERE type = 'task_leg' AND owner_id = 'user_a'")
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_budget_gate_withholds_the_next_leg_at_cap(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Spec A3 (T10): a CONTINUE at/over the budget cap withholds the next leg (the gate paused it).

    The gate returning True means the leg-boundary budget check paused the task and voiced the
    "extend?" ask — so the handler must NOT enqueue the follow-on leg. A gate returning False
    (within cap) continues normally. This is the wiring the audit found missing.
    """
    _seed_active_task(migrated_engine)
    tasks = TaskStore(app_engine)
    before = _leg_job_count(migrated_engine)

    paused_handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_FakeRunnerBuilder(_ContinueRunner()),  # type: ignore[arg-type]
        continuation=TaskContinuation(task_store=tasks, queue=JobQueue(app_engine)),
        budget_gate=lambda _o, _t, _n: _true(),  # over cap → withhold
    )
    await paused_handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER), _FakeContext("user_a")
    )
    assert _leg_job_count(migrated_engine) == before  # no next leg enqueued — withheld at cap

    within_handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_FakeRunnerBuilder(_ContinueRunner()),  # type: ignore[arg-type]
        continuation=TaskContinuation(task_store=tasks, queue=JobQueue(app_engine)),
        budget_gate=lambda _o, _t, _n: _false(),  # within cap → continue
    )
    await within_handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=0, trigger=_TRIGGER), _FakeContext("user_a")
    )
    assert _leg_job_count(migrated_engine) == before + 1  # the next leg WAS enqueued


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False


class _GatedRunner:
    """A leg whose toolbox proposed a gated action → the executor parks WAITING_APPROVAL."""

    async def run(self, task, *, on_event, cancel_token: CancelToken) -> Run:
        from persona.errors import GatedActionProposedError

        raise GatedActionProposedError(
            "gated action awaiting approval",
            context={"proposal_id": "prop-1", "tool": "send_email"},
        )


@pytest.mark.asyncio
async def test_announce_on_park_fires_when_a_leg_gates_an_approval(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Spec A3 (notify-on-park): a leg that parks on an approval proactively voices the ask.

    The wiring the audit found missing: on a WAITING_APPROVAL outcome the handler fires the
    on_approval_parked hook with the proposal id, so the persona voices "may I do X?" instead of
    leaving the user to find the pending approval only in the inbox. Best-effort — the hook never
    fails the (already-parked) leg.
    """
    _seed_active_task(migrated_engine)
    tasks = TaskStore(app_engine)
    announced: list[tuple[str, str]] = []

    async def _spy(owner_id: str, proposal_id: str) -> None:
        announced.append((owner_id, proposal_id))

    handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_FakeRunnerBuilder(_GatedRunner()),  # type: ignore[arg-type]
        continuation=TaskContinuation(task_store=tasks, queue=JobQueue(app_engine)),
        on_approval_parked=_spy,
    )
    await handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER), _FakeContext("user_a")
    )
    assert tasks.get("user_a", "t1").state == TaskState.WAITING  # parked on the user
    assert announced == [("user_a", "prop-1")]  # the ask was voiced proactively


@pytest.mark.asyncio
async def test_handler_resumes_a_waiting_task_on_pickup(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_active_task(migrated_engine)
    tasks = TaskStore(app_engine)
    # Park the task as waiting(on_user) (as A3/A4 would when the leg posed a question).
    TaskContinuation(task_store=tasks, queue=JobQueue(app_engine)).wait_on_user(
        "user_a", "t1", now=_NOW
    )
    assert tasks.get("user_a", "t1").state == TaskState.WAITING

    runner = _FakeRunner()
    handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_FakeRunnerBuilder(runner),
        continuation=TaskContinuation(task_store=tasks, queue=JobQueue(app_engine)),
    )
    # The reply's leg job fires → the handler resumes (waiting→active) on pickup, then runs.
    await handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER), _FakeContext("user_a")
    )
    assert runner.calls == 1  # the leg ran (the task was resumed, not skipped)
    assert tasks.get("user_a", "t1").state == TaskState.COMPLETED  # then the leg completed it
