"""A deploy drain reaches a RUNNING leg, so its work is salvaged (R9-129).

The executor has carried an ``external_cancel`` seam since A2 and two docstrings claimed the
worker's drain signal was wired to it. Nothing ever passed the argument: ``grep -rn
'external_cancel=' packages/*/src`` returned nothing, so ``LegExecutor.run_leg`` always built a
fresh token and a redeploy killed a running leg mid-step, losing the whole leg's work rather
than checkpointing it at the next step boundary.

The user's own controls (cancel, pause) were never the gap: W1's ``_ControlledRunner``
(D-W1-21) reads the durable task row at each boundary and works whichever process pressed the
button. This covers the other half, the one nobody can press: the process going away.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from persona.jobs import JobRegistry
from persona.tasks import Contract, LegBoxLimit, ScheduledFire, Task
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.jobs import Worker
from persona_api.tasks import TaskLegHandler, TaskLegPayload, TaskStore
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.drain import LegDrainSignal
from persona_runtime.agentic.run import CancelToken, Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from persona.tasks import SpendKind, TaskCheckpoint
    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
_OWNER = "user_drain"
_PERSONA = "persona_drain"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "drain.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="d@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


class _Sink:
    """The checkpoint store minus its Postgres-only row id: what landed, and the head."""

    def __init__(self) -> None:
        self.landed: list[TaskCheckpoint] = []

    def get_latest(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:  # noqa: ARG002
        return self.landed[-1] if self.landed else None

    def list_recent(self, owner_id: str, task_id: str, *, limit: int) -> list[TaskCheckpoint]:  # noqa: ARG002
        return list(reversed(self.landed))[:limit]

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int] | None = None,  # noqa: ARG002
        now: datetime,
    ) -> Task:
        self.landed.append(checkpoint)
        return task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)


class _RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


class _Context:
    owner_id = _OWNER
    job_id = "job-drain"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:  # noqa: ARG002
        return None


def test_a_live_leg_is_cancelled_when_the_drain_is_requested() -> None:
    signal = LegDrainSignal()
    with signal.leg_token() as token:
        assert not token.is_cancelled
        signal.request_drain()
        assert token.is_cancelled
        assert token.reason == LegBoxLimit.DRAIN.value


def test_a_leg_starting_during_a_drain_stops_at_its_first_boundary() -> None:
    """The claim loop stops claiming on drain, but a job claimed a moment earlier still runs."""
    signal = LegDrainSignal()
    signal.request_drain()
    with signal.leg_token() as token:
        assert token.is_cancelled
        assert token.reason == LegBoxLimit.DRAIN.value


def test_a_finished_leg_is_released_so_the_registry_does_not_grow() -> None:
    signal = LegDrainSignal()
    with signal.leg_token():
        assert signal.live_legs == 1
    assert signal.live_legs == 0
    signal.request_drain()  # nothing live: must not raise


def test_every_live_leg_is_cancelled_not_merely_the_first() -> None:
    signal = LegDrainSignal()
    with signal.leg_token() as first, signal.leg_token() as second:
        signal.request_drain()
        assert first.is_cancelled
        assert second.is_cancelled


def test_a_leg_running_without_a_drain_is_untouched() -> None:
    signal = LegDrainSignal()
    with signal.leg_token() as token:
        assert not token.is_cancelled
        assert token.reason is None


# --- the wiring: the drain reaches a leg running under the real handler ------------------


class _DrainingRunner:
    """A leg that keeps working until its own token is tripped.

    Step one requests the drain (standing in for the deploy signal arriving mid-leg), then
    the runner checks the token it was handed, exactly as the Spec-06 loop does at a step
    boundary. ``saw_cancel`` is the wiring under test: it can only go true if the handler
    passed the drain's token to the executor as ``external_cancel``.
    """

    def __init__(self, signal: LegDrainSignal) -> None:
        self._signal = signal
        self.runs = 0
        self.saw_cancel = False
        self.steps = 0

    async def run(
        self,
        task: str,
        *,
        on_event: object = None,  # noqa: ARG002
        cancel_token: CancelToken,
        on_step_usage: object = None,  # noqa: ARG002
    ) -> Run:
        self.runs += 1
        for _ in range(5):
            if cancel_token.is_cancelled:
                self.saw_cancel = True
                break
            self.steps += 1
            self._signal.request_drain()  # the deploy signal lands during step one
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.CANCELLED if self.saw_cancel else RunStatus.COMPLETED,
            steps=[Step(type=StepType.REASONING, content="partial work", tokens=10)],
            output=None,
            started_at=_NOW,
            finished_at=_NOW,
        )


class _DrainBuilder:
    def __init__(self, runner: _DrainingRunner) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: object, *, task: object = None) -> object:  # noqa: ARG002
        return self._runner


async def _fire_with_drain(
    engine: Engine, signal: LegDrainSignal | None
) -> tuple[_DrainingRunner, _Sink, _RecordingQueue]:
    store = TaskStore(engine)
    task = Task(
        id="t1",
        owner_id=_OWNER,
        persona_id=_PERSONA,
        contract=Contract(goal="watch the deploy"),
        head_checkpoint_seq=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    store.create(task)
    started = store.start(_OWNER, "t1", now=_NOW)
    sink = _Sink()
    queue = _RecordingQueue()
    runner = _DrainingRunner(signal if signal is not None else LegDrainSignal())
    handler = TaskLegHandler(
        task_store=store,
        checkpoint_store=sink,  # type: ignore[arg-type]
        runner_builder=_DrainBuilder(runner),  # type: ignore[arg-type]
        continuation=TaskContinuation(
            task_store=store,
            queue=queue,  # type: ignore[arg-type]
            checkpoint_store=sink,  # type: ignore[arg-type]
        ),
        drain=signal,
    )
    payload = TaskLegPayload(
        task_id="t1",
        predecessor_seq=started.head_checkpoint_seq,
        trigger=ScheduledFire(schedule_id="s", fire_time=_NOW),
    )
    await handler.handle(payload, _Context())  # type: ignore[arg-type]
    return runner, sink, queue


@pytest.mark.asyncio
async def test_the_drain_reaches_a_leg_running_under_the_handler(engine: Engine) -> None:
    """The fix: the handler passes the drain's token, so the leg stops at its next boundary."""
    signal = LegDrainSignal()
    runner, sink, queue = await _fire_with_drain(engine, signal)

    assert runner.saw_cancel, "the drain never reached the running leg's cancel token"
    assert runner.steps < 5, "the leg ran its whole box instead of stopping at a boundary"
    assert sink.landed, "the leg's work was not salvaged into a checkpoint"
    assert queue.enqueued, "no continuation was enqueued, so the task cannot resume after deploy"


@pytest.mark.asyncio
async def test_a_leg_with_no_drain_signal_runs_to_completion(engine: Engine) -> None:
    """The unchanged path: an install that wires no drain behaves exactly as before."""
    runner, sink, _ = await _fire_with_drain(engine, None)

    assert not runner.saw_cancel
    assert runner.steps == 5
    assert sink.landed


# --- the other end: the worker's own drain is what trips the signal ----------------------


def test_the_worker_tells_the_running_legs_when_it_drains() -> None:
    """R9-129: stopping the claim loop is not enough; a leg already running must be told.

    Without this the signal would exist and nothing would ever call it, which is the shape
    of the defect being fixed rather than the fix.
    """
    signal = LegDrainSignal()
    worker = Worker(
        dispatch_engine=MagicMock(),
        rls_engine=MagicMock(),
        registry=JobRegistry(),
        worker_id="w-drain",
        on_drain=signal.request_drain,
    )
    with signal.leg_token() as token:
        assert not token.is_cancelled
        worker.request_drain()
        assert token.is_cancelled
        assert signal.draining


def test_a_second_drain_signal_is_absorbed() -> None:
    """Fly sends SIGINT then SIGTERM; the second must not re-announce or re-walk the legs."""
    calls: list[int] = []
    worker = Worker(
        dispatch_engine=MagicMock(),
        rls_engine=MagicMock(),
        registry=JobRegistry(),
        worker_id="w-drain",
        on_drain=lambda: calls.append(1),
    )
    worker.request_drain()
    worker.request_drain()
    assert calls == [1]
