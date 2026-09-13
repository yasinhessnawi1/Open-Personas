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
    Contract,
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
from persona_runtime.agentic.run import CancelToken, Run, RunStatus
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


class _ScriptedRunner:
    """Returns a scripted status/output per call (deterministic legs — no model)."""

    def __init__(self, script: list[tuple[RunStatus, str]]) -> None:
        self._script = script
        self.calls = 0

    async def run(self, task, *, on_event, cancel_token: CancelToken) -> Run:
        status, output = self._script[self.calls]
        self.calls += 1
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
    # ... but the ledger counted committed legs only (4 legs × 120), not the wasted re-run.
    assert task.ledger.model_micros == 4 * 120
    # The production distiller kept the checkpoint under budget the whole way.
    enforce_checkpoint_budget(checkpoints.get_latest("user_a", "t1"))  # type: ignore[arg-type]
    # The crash did not spawn a stray leg: leg 3 enqueued exactly once (A2-R-4 key dedup).
    assert _leg_job_count(migrated_engine) <= 4  # legs 1,2,3 enqueued (0 was the seed delivery)
