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
from persona_api.editions import MeteredCreditsPolicy
from persona_api.jobs.queue import JobQueue
from persona_api.tasks import (
    CheckpointStore,
    TaskContinuation,
    TaskLegHandler,
    TaskLegPayload,
    TaskStore,
)
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import CancelToken, Run, RunStatus, StepUsage
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


# --- R9-005: an over-budget checkpoint after a FINISHED run never burns retries ------------------


class _WordyRunner:
    """A counting runner whose single finished output overflows a tiny checkpoint budget."""

    def __init__(self, output: str, *, status: RunStatus = RunStatus.COMPLETED) -> None:
        self.calls = 0
        self._output = output
        self._status = status

    async def run(self, task, *, on_event, cancel_token: CancelToken, on_step_usage=None) -> Run:
        self.calls += 1
        if on_step_usage is not None:
            await on_step_usage(
                StepUsage(
                    step=0,
                    provider="openrouter",
                    model="m",
                    prompt_tokens=100,
                    completion_tokens=100,
                    cost_usd=0.05,
                )
            )
        return Run(
            persona_id="persona_a",
            task=task,
            status=self._status,
            steps=[Step(type=StepType.FINAL, content="done", tokens=100)],
            output=self._output,
            started_at=_NOW,
            finished_at=_NOW,
        )


@pytest.mark.asyncio
async def test_over_budget_checkpoint_parks_task_and_never_retries(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """R9-005 reproduce: the run FINISHES, the checkpoint write overflows the budget — the job
    must SUCCEED (one execution, no retry, no dead-letter) while the task parks honestly
    ``waiting(on_user)`` with the real cause, voiced via ``on_task_stuck``.

    Drives the REAL trigger chain: a queued ``task_leg`` job through the real ``JobExecutor``
    (the retry-classification layer) into the real handler + ``CompactingCheckpointWriter`` +
    ``CheckpointStore`` budget gate — no hand-raised errors, no forced verdicts. Before the
    fix, the store's ``CheckpointTooLargeError`` propagated as transient: 3 full leg re-runs
    (3× model spend), then dead-letter.
    """
    from persona.jobs import JobRegistry, JobState
    from persona_api.jobs.executor import JobExecutor
    from persona_api.tasks import register_task_leg_handler, task_leg_idempotency_key
    from persona_runtime.legs import CompactingCheckpointWriter

    _seed_active_task(migrated_engine)
    tasks = TaskStore(app_engine)
    budget = 24  # tiny: the single finished output alone exceeds it (compaction can't shrink one)
    runner = _WordyRunner("the finished run concluded a very long deliverable indeed " * 20)
    stuck: list[tuple[str, object]] = []

    async def _voice_spy(owner_id: str, report) -> None:
        stuck.append((owner_id, report))

    registry = JobRegistry()
    register_task_leg_handler(
        registry,
        task_store=tasks,
        checkpoint_store=CheckpointStore(app_engine, token_budget=budget),
        runner_builder=_FakeRunnerBuilder(runner),  # type: ignore[arg-type]
        continuation=TaskContinuation(
            task_store=tasks,
            queue=JobQueue(app_engine),
            checkpoint_store=CheckpointStore(app_engine, token_budget=budget),
        ),
        writer=CompactingCheckpointWriter(token_budget=budget),
        on_task_stuck=_voice_spy,
    )
    payload = TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER)
    queue = JobQueue(migrated_engine)
    rec = queue.enqueue(
        type="task_leg",
        owner_id="user_a",
        payload=payload.model_dump(mode="json"),
        idempotency_key=task_leg_idempotency_key(payload),
    )
    assert rec is not None
    executor = JobExecutor(queue=queue, registry=registry, rls_engine=app_engine, worker_id="w1")
    claimed = queue.claim(worker_id="w1", lease_seconds=30, limit=1)
    assert claimed, "expected the enqueued leg job to be claimable"

    outcome = await executor.execute(claimed[0])

    # The job SUCCEEDED — the deterministic write failure was NOT classified transient.
    assert outcome is JobState.SUCCEEDED
    with migrated_engine.begin() as conn:
        state = conn.execute(
            text("SELECT state FROM jobs WHERE id = :i"), {"i": rec.id}
        ).scalar_one()
    assert state == "succeeded"  # not 'queued' (retry), not 'dead' (dead-letter)
    assert runner.calls == 1  # exactly ONE leg execution — no model re-spend
    assert queue.claim(worker_id="w1", lease_seconds=30, limit=5) == []  # nothing re-queued

    # The task parked honestly: waiting(on_user), nothing half-written, the real cause voiced.
    task = tasks.get("user_a", "t1")
    assert task.state == TaskState.WAITING
    assert task.head_checkpoint_seq is None  # the oversized checkpoint never landed
    assert CheckpointStore(app_engine).get_latest("user_a", "t1") is None
    assert len(stuck) == 1
    owner_voiced, report = stuck[0]
    assert owner_voiced == "user_a"
    assert "token budget" in report.cause  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_checkpoint_compaction_keeps_the_task_progressing(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """R9-005 layer 2: with the distiller wired (the new default path), a many-leg task whose
    accumulation would overflow COMPACTS instead — every checkpoint lands, the head advances,
    and the task keeps going (the completed work is preserved, never discarded)."""
    from persona.tasks import checkpoint_token_count
    from persona_runtime.legs import CompactingCheckpointWriter

    _seed_active_task(migrated_engine)
    tasks = TaskStore(app_engine)
    budget = 200
    runner = _WordyRunner(
        "this leg established a fairly wordy conclusion about the ongoing work " * 3,
        status=RunStatus.MAX_STEPS_REACHED,  # not FINAL → CONTINUE (a long multi-leg task)
    )
    handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=CheckpointStore(app_engine, token_budget=budget),
        runner_builder=_FakeRunnerBuilder(runner),  # type: ignore[arg-type]
        writer=CompactingCheckpointWriter(token_budget=budget),
    )
    ctx = _FakeContext("user_a")
    for seq in range(12):
        predecessor = None if seq == 0 else seq - 1
        await handler.handle(
            TaskLegPayload(task_id="t1", predecessor_seq=predecessor, trigger=_TRIGGER), ctx
        )

    task = tasks.get("user_a", "t1")
    assert task.state == TaskState.ACTIVE  # never parked — the budget never tripped
    assert task.head_checkpoint_seq == 11  # every leg's work landed
    latest = CheckpointStore(app_engine).get_latest("user_a", "t1")
    assert latest is not None
    assert checkpoint_token_count(latest) <= budget
    # Older findings were folded into the explicit marker, not lost (restorable via run records).
    assert any("earlier findings compacted" in c for c in latest.progress_conclusions)


# --- Spec M3 (T4b): OWNER-billed, CAS-ridden idempotent leg billing --------------------------


class _BillableRunner:
    """A finished leg that surfaces a priceable per-step usage for the owner-billed deduct.

    ``openrouter`` + ``usage.cost`` 0.03 USD → 3¢ → ceil(3.0) = 3 credits at markup 1.0.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, task, *, on_event, cancel_token: CancelToken, on_step_usage=None) -> Run:
        self.calls += 1
        await on_event(RunEvent.thinking(0))
        if on_step_usage is not None:
            await on_step_usage(
                StepUsage(
                    step=0,
                    provider="openrouter",
                    model="openai/gpt-5.4-image-2",
                    prompt_tokens=100,
                    completion_tokens=1000,
                    cost_usd=0.03,
                )
            )
        return Run(
            persona_id="persona_a",
            task=task,
            status=RunStatus.COMPLETED,
            steps=[Step(type=StepType.FINAL, content="done", tokens=1100)],
            output="done",
            started_at=_NOW,
            finished_at=_NOW,
        )


def _task_leg_ledger(engine: Engine, owner: str) -> list[tuple[int, str, object, object]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT delta, reason, cost_basis, billing_key FROM credit_transactions "
                "WHERE user_id = :u AND reason LIKE 'task_leg%' ORDER BY created_at, id"
            ),
            {"u": owner},
        ).all()
    return [(int(r[0]), str(r[1]), r[2], r[3]) for r in rows]


def _billing_handler(app_engine: Engine, bill_engine: Engine, runner: object) -> TaskLegHandler:
    return TaskLegHandler(
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_FakeRunnerBuilder(runner),  # type: ignore[arg-type]
        credits_policy=MeteredCreditsPolicy(),
        rls_engine=bill_engine,  # superuser engine bypasses RLS for the ledger write
        cost_source=None,  # static default; the OpenRouter actual prices regardless
    )


@pytest.mark.asyncio
async def test_redelivery_does_not_double_charge_the_owner(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    # THE explicit acceptance: a re-delivered leg (same task_id/checkpoint_seq) charges the owner
    # ONCE — the ON CONFLICT (billing_key) gate — while A0 meters BOTH executions to audit.
    _seed_active_task(migrated_engine)
    runner = _BillableRunner()
    handler = _billing_handler(app_engine, migrated_engine, runner)
    ctx = _FakeContext("user_a")
    payload = TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER)

    await handler.handle(payload, ctx)  # first delivery
    await handler.handle(payload, ctx)  # forced re-delivery (SAME payload → same seq 0)

    rows = _task_leg_ledger(migrated_engine, "user_a")
    assert len(rows) == 1, f"owner must be billed exactly ONCE, not per re-delivery; got {rows}"
    delta, reason, basis, billing_key = rows[0]
    assert delta == -3  # ceil(3.0¢) at markup 1.0
    assert reason == "task_leg:actual_openrouter"
    assert basis == "actual_openrouter"
    assert billing_key == "t1:leg:0"  # the checkpoint's identity — the CAS-ridden key
    # The leg genuinely RE-RAN (at-least-once) and A0 metered BOTH executions (forensics)...
    assert runner.calls == 2
    assert ctx.meter_calls == [1100, 1100]
    # ...but the owner credit ledger accrued exactly once (the billing_key gate).


@pytest.mark.asyncio
async def test_normal_leg_bills_the_owner_once_at_real_cost(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_active_task(migrated_engine)
    handler = _billing_handler(app_engine, migrated_engine, _BillableRunner())
    await handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER), _FakeContext("user_a")
    )
    rows = _task_leg_ledger(migrated_engine, "user_a")
    assert len(rows) == 1
    assert rows[0][0] == -3  # the real Sonnet-class cost, floored — not a flat fee


@pytest.mark.asyncio
async def test_over_budget_checkpoint_park_bills_the_owner_nothing(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    # R9-005 + T4b: the run FINISHES but the checkpoint overflows the budget → the handler parks
    # (no append landed) → NO owner credit is charged (bills nothing extra).
    from persona_runtime.legs import CompactingCheckpointWriter

    _seed_active_task(migrated_engine)
    budget = 24
    handler = TaskLegHandler(
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine, token_budget=budget),
        runner_builder=_FakeRunnerBuilder(_WordyRunner("a very long finished deliverable " * 20)),  # type: ignore[arg-type]
        writer=CompactingCheckpointWriter(token_budget=budget),
        credits_policy=MeteredCreditsPolicy(),
        rls_engine=migrated_engine,
        cost_source=None,
    )
    await handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER), _FakeContext("user_a")
    )
    # The task parked (over-budget checkpoint never landed) → no committed leg → no owner bill.
    assert _task_leg_ledger(migrated_engine, "user_a") == []


def test_default_writer_is_the_compacting_distiller() -> None:
    """R9-005 wiring guard: a handler built without an explicit writer gets the T12 distiller,
    never the unbounded ``BasicCheckpointWriter`` stand-in that caused the production
    dead-letter."""
    from persona_runtime.legs import CompactingCheckpointWriter

    handler = TaskLegHandler(
        task_store=None,  # type: ignore[arg-type]  # wiring-only check; never handles a job
        checkpoint_store=None,  # type: ignore[arg-type]
        runner_builder=None,  # type: ignore[arg-type]
    )
    assert isinstance(handler._writer, CompactingCheckpointWriter)  # noqa: SLF001
