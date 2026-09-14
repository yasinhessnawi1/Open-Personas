"""A real multi-leg task with a forced crash-resume (Spec A2, T12; criteria 2-5).

End-to-end on the real stack (real Postgres, real ``TaskStore`` / ``CheckpointStore`` / CAS,
real ``TaskLegHandler`` + ``TaskContinuation`` + the production ``CompactingCheckpointWriter``).
A task runs 4 legs to completion; a leg is **re-delivered** mid-task — the honest model of an
A0 lease-expiry reclaim (a crashed worker's job comes back), NOT a hand-flip to a resume state.
The proof: the re-delivery resumes from the last checkpoint (no double checkpoint, no double
spend, no double-completion) via the store CAS, and the task still finishes coherently.

The full DEPLOYED-worker kill (a real SIGKILL'd worker loop) is the operator pass at deploy —
orchestrator-owned, exactly as A0's Fly crash-resume + Shape-2 soak were (closeout).
"""

# ruff: noqa: ARG001, ARG002, ANN001 — fixture-ordering + Protocol-conformance test stubs.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.tasks import (
    AcceptanceCriterion,
    AcceptanceStatus,
    Contract,
    CriterionClaim,
    ScheduledFire,
    Task,
    TaskState,
    enforce_checkpoint_budget,
)
from persona_api.jobs.queue import JobQueue
from persona_api.tasks import (
    CheckpointStore,
    TaskContinuation,
    TaskLegHandler,
    TaskLegPayload,
    TaskStore,
)
from persona_runtime.agentic.run import CancelToken, Run, RunStatus, StepUsage
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import CompactingCheckpointWriter
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)
_TRIGGER = ScheduledFire(schedule_id="sched", fire_time=_NOW)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


#: What one scripted leg costs: an OpenRouter actual of 0.002 USD = 0.2¢ = 20 ledger micros
#: (R9-161). Not equal to the run's 120 tokens, so the ledger assertions below fail if the
#: ledger ever goes back to carrying a token count.
_LEG_COST_USD = 0.002
_LEG_COST_MICROS = 20


class _ScriptedRunner:
    """Returns a scripted status/output per call (deterministic legs — no model)."""

    def __init__(self, script: list[tuple[RunStatus, str]]) -> None:
        self._script = script
        self.calls = 0
        self.reconstructions: list[str] = []

    async def run(self, task, *, on_event, cancel_token: CancelToken, on_step_usage=None) -> Run:
        status, output = self._script[self.calls]
        self.calls += 1
        self.reconstructions.append(task)
        if on_step_usage is not None:
            await on_step_usage(
                StepUsage(
                    step=0,
                    provider="openrouter",
                    model="z-ai/glm-4.6",
                    prompt_tokens=80,
                    completion_tokens=40,
                    cost_usd=_LEG_COST_USD,
                )
            )
        has_output = status in (RunStatus.COMPLETED, RunStatus.MAX_STEPS_REACHED)
        return Run(
            persona_id="persona_a",
            task=task,
            status=status,
            steps=[Step(type=StepType.FINAL, content=output, tokens=120)],
            output=output if has_output else None,
            started_at=_NOW,
            finished_at=_NOW,
        )


class _Builder:
    def __init__(self, runner: _ScriptedRunner) -> None:
        self._runner = runner

    def build(self, task_id, persona_id, box, *, task=None) -> _ScriptedRunner:
        return self._runner


class _Ctx:
    def __init__(self, owner: str) -> None:
        self._owner = owner

    @property
    def owner_id(self) -> str:
        return self._owner

    @property
    def job_id(self) -> str:
        return "job"

    def meter(self, *, amount_micros: int, kind: str, detail=None) -> None:
        return None


def _seed(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('user_a','a@example.com')"))
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES ('persona_a','user_a','name: x')"
            )
        )
    tasks = TaskStore(engine)
    tasks.create(
        Task(
            id="t1",
            owner_id="user_a",
            persona_id="persona_a",
            contract=Contract(goal="track flights"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start("user_a", "t1", now=_NOW)


def _leg_job_count(engine: Engine) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text("SELECT count(*) FROM jobs WHERE type='task_leg' AND owner_id='user_a'")
        ).scalar_one()


@pytest.mark.asyncio
async def test_multileg_task_with_crash_resume(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine)
    # Legs 0,1,2 continue (MAX_STEPS); the re-delivered leg 2 continues again; leg 3 completes.
    cont_, done_ = RunStatus.MAX_STEPS_REACHED, RunStatus.COMPLETED
    runner = _ScriptedRunner(
        [
            (cont_, "leg0: surveyed airlines"),
            (cont_, "leg1: tracked SAS at 1620kr"),
            (cont_, "leg2: best so far 1480kr Norwegian"),
            (cont_, "leg2 REDELIVERED: best so far 1480kr Norwegian"),  # the crash re-run
            (done_, "leg3: booked the 1480kr fare"),
        ]
    )
    tasks = TaskStore(app_engine)
    checkpoints = CheckpointStore(app_engine)
    continuation = TaskContinuation(
        task_store=tasks, queue=JobQueue(app_engine), checkpoint_store=checkpoints
    )
    handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=checkpoints,
        runner_builder=_Builder(runner),
        continuation=continuation,
        writer=CompactingCheckpointWriter(),  # the PRODUCTION distiller, not the stand-in
    )
    ctx = _Ctx("user_a")

    async def deliver(predecessor: int | None) -> None:
        await handler.handle(
            TaskLegPayload(task_id="t1", predecessor_seq=predecessor, trigger=_TRIGGER), ctx
        )

    await deliver(None)  # leg 0  → checkpoint 0, head 0
    await deliver(0)  # leg 1  → checkpoint 1, head 1
    await deliver(1)  # leg 2  → checkpoint 2, head 2
    # CRASH: leg 2's job comes back (lease-expiry reclaim). Same payload → same seq → CAS no-op.
    await deliver(1)  # leg 2 RE-DELIVERED → resumes from checkpoint 2, no double-write
    await deliver(2)  # leg 3  → COMPLETED → continuation completes the task

    task = tasks.get("user_a", "t1")
    assert task.state == TaskState.COMPLETED  # coherent finish despite the crash
    assert task.head_checkpoint_seq == 3
    cps = checkpoints.list_recent("user_a", "t1", limit=20)
    assert len(cps) == 4  # exactly 4 (0-3) — the re-delivery wrote NO duplicate
    assert runner.calls == 5  # the leg re-ran on re-delivery (at-least-once) ...
    # ... but the ledger counted committed legs only (4 legs of real cost), not the re-run.
    assert task.ledger.model_micros == 4 * _LEG_COST_MICROS
    # The production distiller kept the checkpoint under budget the whole way.
    enforce_checkpoint_budget(checkpoints.get_latest("user_a", "t1"))  # type: ignore[arg-type]
    # The crash did not spawn a stray leg: leg 3 enqueued exactly once (A2-R-4 key dedup).
    assert _leg_job_count(migrated_engine) <= 4  # legs 1,2,3 enqueued (0 was the seed delivery)


@pytest.mark.asyncio
async def test_the_obstacle_a_park_recorded_reaches_the_next_legs_window(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """R9-163, driven through the real chain rather than a hand-built checkpoint.

    Leg 0 works and continues. Leg 1 hits the A3 approval gate, so it executes nothing and
    writes no checkpoint of its own, and the park records WHAT it is waiting for. The leg
    after the approval must be TOLD that: before this the continuity window read a leg's
    outcome off ``blocked_on``, nothing ever wrote one, and every leg in every window was
    summarised as "worked" — including the ones that had got nowhere.
    """
    _seed(migrated_engine)
    runner = _GatingThenWorkingRunner(
        [
            (RunStatus.MAX_STEPS_REACHED, "leg0: the portal has a 2FA step"),
            None,  # leg 1 gates on a policy-gated action
            (RunStatus.MAX_STEPS_REACHED, "leg2: sent it once you said yes"),
            (RunStatus.COMPLETED, "leg3: pulled the statement"),
        ]
    )
    tasks = TaskStore(app_engine)
    checkpoints = CheckpointStore(app_engine)
    continuation = TaskContinuation(
        task_store=tasks, queue=JobQueue(app_engine), checkpoint_store=checkpoints
    )
    handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=checkpoints,
        runner_builder=_Builder(runner),
        continuation=continuation,
        writer=CompactingCheckpointWriter(),
    )
    ctx = _Ctx("user_a")

    async def deliver(predecessor: int | None) -> None:
        await handler.handle(
            TaskLegPayload(task_id="t1", predecessor_seq=predecessor, trigger=_TRIGGER), ctx
        )

    await deliver(None)  # leg 0 → checkpoint 0
    await deliver(0)  # leg 1 gates → the park records the obstacle as checkpoint 1
    assert tasks.get("user_a", "t1").state == TaskState.WAITING

    tasks.resume("user_a", "t1", now=_NOW)
    await deliver(1)  # the leg after the approval reads the obstacle as its CHECKPOINT block

    assert "BLOCKED ON: Waiting for your approval: Send an email" in runner.reconstructions[2]

    await deliver(2)  # one leg later the park has fallen back into the continuity window

    assert "blocked: Waiting for your approval: Send an email" in runner.reconstructions[3]
    # And the legs that ran are not described as blocked: the obstacle cleared itself.
    head = checkpoints.get_latest("user_a", "t1")
    assert head is not None
    assert head.blocked_on is None


class _GatingThenWorkingRunner(_ScriptedRunner):
    """A scripted runner where a ``None`` entry means "this leg hits the A3 gate".

    The gate is a control-flow signal raised by the policy-gated toolbox and propagated
    through the unmodified loop, so a runner that raises it is the honest stand-in for a leg
    whose action needed permission it did not have.
    """

    async def run(self, task, *, on_event, cancel_token: CancelToken, on_step_usage=None) -> Run:
        from persona.errors import GatedActionProposedError

        if self._script[self.calls] is None:
            self.calls += 1
            self.reconstructions.append(task)
            # A gated leg raises BEFORE any step usage, which is why its spend is empty.
            raise GatedActionProposedError(
                "gated action awaiting approval",
                context={
                    "proposal_id": "prop_1",
                    "tool": "send_email",
                    "description": "Send an email to the landlord",
                },
            )
        return await super().run(
            task, on_event=on_event, cancel_token=cancel_token, on_step_usage=on_step_usage
        )


# --- R9-164: a leg's work ticks the contract's checklist ---------------------


class _WritingRunner:
    """A leg that persists a file, the way a real ``file_write`` leg does (Spec 28)."""

    def __init__(self, path: str, status: RunStatus = RunStatus.COMPLETED) -> None:
        self._path = path
        self._status = status

    async def run(self, task, *, on_event, cancel_token: CancelToken, on_step_usage=None) -> Run:
        from persona.schema.tools import PersistedArtifact, ToolCall, ToolResult

        if on_step_usage is not None:
            await on_step_usage(
                StepUsage(
                    step=0,
                    provider="openrouter",
                    model="z-ai/glm-4.6",
                    prompt_tokens=80,
                    completion_tokens=40,
                    cost_usd=_LEG_COST_USD,
                )
            )
        return Run(
            persona_id="persona_a",
            task=task,
            status=self._status,
            steps=[
                Step(
                    type=StepType.TOOL_CALL,
                    tool_calls=[
                        ToolCall(name="file_write", args={"path": self._path}, call_id="a")
                    ],
                    results=[
                        ToolResult(
                            tool_name="file_write",
                            call_id="a",
                            content=f"wrote {self._path}",
                            artifacts=(
                                PersistedArtifact(
                                    workspace_path=self._path,
                                    mime_type="text/markdown",
                                    size_bytes=64,
                                ),
                            ),
                        )
                    ],
                    tokens=120,
                )
            ],
            output="the comparison is written",
            started_at=_NOW,
            finished_at=_NOW,
        )


class _ScriptedAssessor:
    """Claims exactly what the test tells it to (the model half is unit-tested elsewhere)."""

    def __init__(self, claims: tuple[CriterionClaim, ...]) -> None:
        self._claims = claims
        self.contracts_seen: list[Contract] = []

    async def assess(self, *, contract: Contract, run) -> tuple[CriterionClaim, ...]:
        self.contracts_seen.append(contract)
        return self._claims


def _seed_with_criteria(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('user_a','a@example.com')"))
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES ('persona_a','user_a','name: x')"
            )
        )
    tasks = TaskStore(engine)
    tasks.create(
        Task(
            id="t1",
            owner_id="user_a",
            persona_id="persona_a",
            contract=Contract(
                goal="compare rental deposit schemes",
                acceptance_criteria=(
                    AcceptanceCriterion(id="c1", statement="written to a file"),
                    AcceptanceCriterion(id="c2", statement="three schemes compared"),
                ),
            ),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start("user_a", "t1", now=_NOW)


def _handler_with(
    app_engine: Engine, runner: object, assessor: _ScriptedAssessor
) -> TaskLegHandler:
    tasks = TaskStore(app_engine)
    checkpoints = CheckpointStore(app_engine)
    return TaskLegHandler(
        task_store=tasks,
        checkpoint_store=checkpoints,
        runner_builder=_Builder(runner),  # type: ignore[arg-type]
        continuation=TaskContinuation(
            task_store=tasks, queue=JobQueue(app_engine), checkpoint_store=checkpoints
        ),
        writer=CompactingCheckpointWriter(),
        acceptance=assessor,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_an_evidenced_criterion_advances_and_the_next_leg_reads_it_as_done(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The whole chain: the leg writes a file, the claim cites it, the contract moves.

    Before this AcceptanceStatus.DONE was unreachable, so a task re-read its whole checklist
    as ``[pending]`` on every leg for as long as it lived.
    """
    _seed_with_criteria(migrated_engine)
    assessor = _ScriptedAssessor(
        (
            CriterionClaim(
                criterion_id="c1",
                status=AcceptanceStatus.DONE,
                evidence="wrote reports/deposits.md",
            ),
        )
    )
    handler = _handler_with(app_engine, _WritingRunner("reports/deposits.md"), assessor)

    await handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER), _Ctx("user_a")
    )

    criteria = TaskStore(app_engine).get("user_a", "t1").contract.acceptance_criteria
    assert [(c.id, c.status) for c in criteria] == [
        ("c1", AcceptanceStatus.DONE),
        ("c2", AcceptanceStatus.PENDING),
    ]


@pytest.mark.asyncio
async def test_an_unevidenced_claim_leaves_the_checklist_alone(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The gate runs inside the store's write, so a claim cannot route around it."""
    _seed_with_criteria(migrated_engine)
    assessor = _ScriptedAssessor(
        (
            CriterionClaim(
                criterion_id="c2",
                status=AcceptanceStatus.DONE,
                evidence="I compared three schemes, trust me",
            ),
        )
    )
    handler = _handler_with(app_engine, _WritingRunner("reports/deposits.md"), assessor)

    await handler.handle(
        TaskLegPayload(task_id="t1", predecessor_seq=None, trigger=_TRIGGER), _Ctx("user_a")
    )

    criteria = TaskStore(app_engine).get("user_a", "t1").contract.acceptance_criteria
    assert all(c.status is AcceptanceStatus.PENDING for c in criteria)
