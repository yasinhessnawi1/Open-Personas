"""A task at its leg cap or past its deadline is parked, not re-enqueued (finding O).

``ContractBounds.deadline`` and ``max_legs`` were advertised on every task by the REST API
and nothing could set or enforce them. The judge now sets them; this is the enforcing half,
driven through the real leg handler on the community engine: the same leg-boundary point the
budget gate uses, and the same park the approval gate and the over-budget checkpoint use
(``waiting(on_user)`` with the reason on the head checkpoint), never a new mechanism.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.tasks import (
    Contract,
    ContractBounds,
    ScheduledFire,
    Task,
    TaskCheckpoint,
    TaskState,
    UserDispatch,
    WaitKind,
)
from persona.tasks.checkpoint_kind import CheckpointKind, checkpoint_id, checkpoint_kind
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.tasks import TaskLegHandler, TaskLegPayload, TaskStore
from persona_api.tasks.continuation import TaskContinuation
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from persona.tasks import LegBox, ResumeTrigger, SpendKind
    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
_OWNER = "user_bounds"
_PERSONA = "persona_bounds"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "bounds.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="b@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


class _BoxedRunner:
    """A leg the box stopped: the shape whose successor the task enqueues itself."""

    def __init__(self) -> None:
        self.runs = 0

    async def run(self, task: str, **_: object) -> Run:
        self.runs += 1
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.MAX_STEPS_REACHED,
            steps=[Step(type=StepType.REASONING, content="still looking", tokens=10)],
            output=None,
            started_at=_NOW,
            finished_at=_NOW,
        )


class _Builder:
    def __init__(self, runner: _BoxedRunner) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: LegBox, *, task: object = None) -> object:  # noqa: ARG002
        return self._runner


class _Sink:
    """The checkpoint store minus its Postgres-only row id: what landed, and the head."""

    def __init__(self, chain: tuple[CheckpointKind, ...] = ()) -> None:
        self.landed: list[TaskCheckpoint] = [
            TaskCheckpoint(
                task_id="t1",
                leg_id=checkpoint_id("t1", kind, seq),
                checkpoint_seq=seq,
                updated_at=_NOW,
            )
            for seq, kind in enumerate(chain)
        ]

    def count_legs(self, owner_id: str, task_id: str) -> int:  # noqa: ARG002
        return sum(
            1 for c in self.landed if checkpoint_kind(task_id, c.leg_id) is CheckpointKind.LEG
        )

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
    job_id = "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:  # noqa: ARG002
        return None


def _task(store: TaskStore, bounds: ContractBounds, *, chain_length: int = 0) -> Task:
    """A started task whose checkpoint chain is ``chain_length`` long (its head says so)."""
    task = Task(
        id="t1",
        owner_id=_OWNER,
        persona_id=_PERSONA,
        contract=Contract(goal="find a flat", bounds=bounds),
        head_checkpoint_seq=chain_length - 1 if chain_length else None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    store.create(task)
    return store.start(_OWNER, "t1", now=_NOW)


async def _fire(
    engine: Engine,
    bounds: ContractBounds,
    trigger: ResumeTrigger,
    *,
    chain: tuple[CheckpointKind, ...] = (),
) -> tuple[TaskStore, _Sink, _RecordingQueue, _BoxedRunner]:
    store = TaskStore(engine)
    task = _task(store, bounds, chain_length=len(chain))
    sink = _Sink(chain)
    queue = _RecordingQueue()
    runner = _BoxedRunner()
    handler = TaskLegHandler(
        task_store=store,
        checkpoint_store=sink,  # type: ignore[arg-type]
        runner_builder=_Builder(runner),  # type: ignore[arg-type]
        continuation=TaskContinuation(
            task_store=store,
            queue=queue,  # type: ignore[arg-type]
            checkpoint_store=sink,  # type: ignore[arg-type]
        ),
    )
    payload = TaskLegPayload(
        task_id="t1", predecessor_seq=task.head_checkpoint_seq, trigger=trigger
    )
    await handler.handle(payload, _Context())  # type: ignore[arg-type]
    return store, sink, queue, runner


@pytest.mark.asyncio
async def test_a_task_at_its_leg_cap_is_parked_rather_than_re_enqueued(engine: Engine) -> None:
    """ "Give it at most two legs": the second leg runs, and then the task waits on the user
    with the cap as its stated reason, where the budget gate would have enqueued leg three."""
    store, sink, queue, runner = await _fire(
        engine,
        ContractBounds(max_legs=2),
        ScheduledFire(schedule_id="s", fire_time=_NOW),
        chain=(CheckpointKind.LEG,),
    )

    assert runner.runs == 1  # this leg ran; it was the last one allowed
    assert queue.enqueued == []  # and nothing followed it
    parked = store.get(_OWNER, "t1")
    assert parked.state is TaskState.WAITING
    assert parked.wait_kind is WaitKind.ON_USER
    head = sink.get_latest(_OWNER, "t1")
    assert head is not None
    assert head.blocked_on is not None
    assert "2 legs" in head.blocked_on


@pytest.mark.asyncio
async def test_a_task_under_its_leg_cap_continues(engine: Engine) -> None:
    _, _, queue, runner = await _fire(
        engine, ContractBounds(max_legs=3), ScheduledFire(schedule_id="s", fire_time=_NOW)
    )

    assert runner.runs == 1
    assert len(queue.enqueued) == 1


@pytest.mark.asyncio
async def test_a_fire_after_the_deadline_parks_before_spending_a_leg(engine: Engine) -> None:
    """A recurring task's occurrences complete rather than continue, so the boundary check
    alone would never see the deadline on one. The clock-driven leg is refused at the door,
    with the reason, instead of running one more time past "until Friday"."""
    store, sink, queue, runner = await _fire(
        engine,
        ContractBounds(deadline=_NOW - timedelta(days=30)),
        ScheduledFire(schedule_id="s", fire_time=_NOW),
    )

    assert runner.runs == 0
    assert queue.enqueued == []
    parked = store.get(_OWNER, "t1")
    assert parked.state is TaskState.WAITING
    assert parked.wait_kind is WaitKind.ON_USER
    head = sink.get_latest(_OWNER, "t1")
    assert head is not None
    assert head.blocked_on is not None
    assert "deadline" in head.blocked_on


@pytest.mark.asyncio
async def test_a_user_pickup_past_the_deadline_runs_one_more_leg(engine: Engine) -> None:
    """The person's own hand is not the clock: picking a parked task up runs a leg. The
    boundary check then parks it again, so a pickup is exactly one more leg."""
    store, _, queue, runner = await _fire(
        engine,
        ContractBounds(deadline=_NOW - timedelta(days=30)),
        UserDispatch(dispatched_at=_NOW),
    )

    assert runner.runs == 1
    assert queue.enqueued == []
    assert store.get(_OWNER, "t1").wait_kind is WaitKind.ON_USER


@pytest.mark.asyncio
async def test_records_between_legs_do_not_count_against_the_leg_cap(engine: Engine) -> None:
    """R9-220: one leg ran, then an approval park recorded its obstacle and the answer was
    folded in. The head is at 2, but only one leg went at the work, so a cap of three still
    has room for this leg and the next. Counting the head parked it at the door."""
    _, _, queue, runner = await _fire(
        engine,
        ContractBounds(max_legs=3),
        ScheduledFire(schedule_id="s", fire_time=_NOW),
        chain=(CheckpointKind.LEG, CheckpointKind.BLOCKED, CheckpointKind.APPROVAL),
    )

    assert runner.runs == 1
    assert len(queue.enqueued) == 1


@pytest.mark.asyncio
async def test_a_saved_reply_does_not_count_against_the_leg_cap_either(engine: Engine) -> None:
    """The fourth record kind (R9-240's saved reply) is not a leg: two legs plus a reply
    under a cap of three runs this leg, which is the third, and then parks."""
    store, _, queue, runner = await _fire(
        engine,
        ContractBounds(max_legs=3),
        ScheduledFire(schedule_id="s", fire_time=_NOW),
        chain=(CheckpointKind.LEG, CheckpointKind.REPLY, CheckpointKind.LEG),
    )

    assert runner.runs == 1
    assert queue.enqueued == []
    assert store.get(_OWNER, "t1").wait_kind is WaitKind.ON_USER


def test_the_store_counts_the_legs_in_a_chain_not_its_length(engine: Engine) -> None:
    """The real query, on the community engine: five checkpoints, two of them legs.

    The rows are written directly because the community schema has no server default for
    the checkpoint row id (the Postgres-only part ``_Sink`` stands in for above); the count
    reads ``leg_id`` out of the stored JSON exactly as it does on Postgres.
    """
    from persona_api.db.models import task_checkpoints as checkpoints_t
    from persona_api.tasks.serde import checkpoint_values
    from persona_api.tasks.store import CheckpointStore

    _task(TaskStore(engine), ContractBounds(max_legs=9))
    kinds = (
        CheckpointKind.LEG,
        CheckpointKind.BLOCKED,
        CheckpointKind.APPROVAL,
        CheckpointKind.LEG,
        CheckpointKind.REPLY,
    )
    with engine.begin() as conn:
        for seq, kind in enumerate(kinds):
            checkpoint = TaskCheckpoint(
                task_id="t1",
                leg_id=checkpoint_id("t1", kind, seq),
                checkpoint_seq=seq,
                updated_at=_NOW,
            )
            conn.execute(
                insert(checkpoints_t).values(id=f"cp{seq}", **checkpoint_values(checkpoint, _OWNER))
            )

    assert CheckpointStore(engine).count_legs(_OWNER, "t1") == 2
