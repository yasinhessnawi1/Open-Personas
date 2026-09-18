"""A task leaves a trace in the persona's own memory (Spec A2, T10; D-A2-4).

``MilestoneRecorder``, ``milestone_for`` and ``TaskEpisodicSink`` were written, tested and
called by nothing: a persona that had run a task for five days could not afterwards say that
it had. Asked "did you ever look into X?" it had no narrative trace that the task started,
progressed, waited on the person, completed or failed.

These drive the REAL leg handler and the REAL continuation on the community engine, with the
episodic store faked and nothing else, because the point is the wiring: which milestone each
real outcome produces, that the note names the task, and that a store the leg cannot write to
costs the leg nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.errors import TaskLegFailedError
from persona.schema.chunks import PersonaChunk
from persona.tasks import (
    Contract,
    ContractBounds,
    ScheduledFire,
    Task,
    TaskCheckpoint,
    TaskState,
    WaitKind,
)
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.tasks import TaskLegHandler, TaskLegPayload, TaskStore
from persona_api.tasks.continuation import TaskContinuation
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import MilestoneRecorder
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from persona.tasks import LegBox, SpendKind
    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
_OWNER = "user_milestones"
_PERSONA = "persona_milestones"
_GOAL = "find a two-bedroom flat in Kristiansand"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "milestones.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="m@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


class _FakeEpisodic:
    """The persona's episodic store, minus Postgres. Optionally a broken one."""

    def __init__(self, *, broken: bool = False) -> None:
        self.chunks: list[PersonaChunk] = []
        self.personas: list[str] = []
        self._broken = broken

    def write(self, persona_id: str, chunks: list[PersonaChunk], **_: object) -> None:
        if self._broken:
            msg = "episodic store unreachable"
            raise RuntimeError(msg)
        self.personas.append(persona_id)
        self.chunks.extend(chunks)

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:  # noqa: ARG002
        return list(self.chunks)

    @property
    def milestones(self) -> list[str]:
        return [c.metadata["milestone"] for c in self.chunks]


class _Runner:
    """A leg that ends however the test needs it to."""

    def __init__(self, status: RunStatus, output: str | None) -> None:
        self._status = status
        self._output = output
        self.runs = 0

    async def run(self, task: str, **_: object) -> Run:
        self.runs += 1
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=self._status,
            steps=[Step(type=StepType.REASONING, content="looked around", tokens=10)],
            output=self._output,
            error="the search tool blew up" if self._status is RunStatus.ERROR else None,
            started_at=_NOW,
            finished_at=_NOW,
        )


class _Builder:
    def __init__(self, runner: _Runner) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: LegBox, *, task: object = None) -> object:  # noqa: ARG002
        return self._runner


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
    job_id = "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:  # noqa: ARG002
        return None


def _task(store: TaskStore, *, legs_done: int, bounds: ContractBounds | None) -> Task:
    """A started task that has already run ``legs_done`` legs (its head says so)."""
    task = Task(
        id="t1",
        owner_id=_OWNER,
        persona_id=_PERSONA,
        contract=Contract(goal=_GOAL, bounds=bounds or ContractBounds()),
        head_checkpoint_seq=legs_done - 1 if legs_done else None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    store.create(task)
    return store.start(_OWNER, "t1", now=_NOW)


async def _fire(
    engine: Engine,
    *,
    status: RunStatus = RunStatus.MAX_STEPS_REACHED,
    output: str | None = "three listings under 12000kr",
    legs_done: int = 0,
    bounds: ContractBounds | None = None,
    prior_conclusions: tuple[str, ...] = (),
    episodic: _FakeEpisodic | None = None,
) -> tuple[TaskStore, _FakeEpisodic, _RecordingQueue, _Runner]:
    """Run one real leg through the real handler and hand back what it remembered."""
    store = TaskStore(engine)
    task = _task(store, legs_done=legs_done, bounds=bounds)
    sink = _Sink()
    if prior_conclusions:
        # The head this leg builds on, so ``milestone_for`` has something to compare against.
        sink.landed.append(
            TaskCheckpoint(
                task_id="t1",
                leg_id="t1:leg:0",
                checkpoint_seq=0,
                progress_conclusions=prior_conclusions,
                next_step="",
                updated_at=_NOW,
            )
        )
    remembered = episodic if episodic is not None else _FakeEpisodic()
    recorder = MilestoneRecorder(remembered)  # type: ignore[arg-type]
    queue = _RecordingQueue()
    runner = _Runner(status, output)
    handler = TaskLegHandler(
        task_store=store,
        checkpoint_store=sink,  # type: ignore[arg-type]
        runner_builder=_Builder(runner),  # type: ignore[arg-type]
        continuation=TaskContinuation(
            task_store=store,
            queue=queue,  # type: ignore[arg-type]
            checkpoint_store=sink,  # type: ignore[arg-type]
            milestones=recorder,
        ),
        milestones=recorder,
    )
    payload = TaskLegPayload(
        task_id="t1",
        predecessor_seq=task.head_checkpoint_seq,
        trigger=ScheduledFire(schedule_id="s", fire_time=_NOW),
    )
    await handler.handle(payload, _Context())  # type: ignore[arg-type]
    return store, remembered, queue, runner


def _only(remembered: _FakeEpisodic) -> PersonaChunk:
    assert len(remembered.chunks) == 1, remembered.milestones
    return remembered.chunks[0]


@pytest.mark.asyncio
async def test_the_first_leg_is_remembered_as_the_task_starting(engine: Engine) -> None:
    """The persona can say it took the job on, which is the first thing anyone asks about."""
    _, remembered, _, runner = await _fire(engine)

    assert runner.runs == 1
    chunk = _only(remembered)
    assert chunk.metadata["milestone"] == "task_started"
    assert chunk.metadata["source"] == "task_milestone"  # the consolidation filter reads this
    assert chunk.metadata["task_id"] == "t1"
    assert remembered.personas == [_PERSONA]  # the persona's store, not the task-scoped sink
    assert _GOAL in chunk.text
    assert "started" in chunk.text


@pytest.mark.asyncio
async def test_a_leg_that_reaches_a_new_conclusion_is_remembered_as_progress(
    engine: Engine,
) -> None:
    """Not the first leg, so it earns its note by having learned something."""
    _, remembered, _, _ = await _fire(
        engine,
        legs_done=1,
        prior_conclusions=("nothing yet",),
        output="three listings under 12000kr",
    )

    chunk = _only(remembered)
    assert chunk.metadata["milestone"] == "major_progress"
    assert _GOAL in chunk.text
    assert "three listings under 12000kr" in chunk.text  # what it learned, not just that it ran


@pytest.mark.asyncio
async def test_a_leg_that_learns_nothing_new_is_not_remembered(engine: Engine) -> None:
    """The restraint half of D-A2-4: an ordinary continuation is not a memory."""
    _, remembered, _, _ = await _fire(
        engine, legs_done=1, prior_conclusions=("nothing yet",), output=None
    )

    assert remembered.chunks == []


@pytest.mark.asyncio
async def test_a_completed_task_is_remembered_as_completed(engine: Engine) -> None:
    _, remembered, _, _ = await _fire(
        engine,
        status=RunStatus.COMPLETED,
        legs_done=1,
        prior_conclusions=("nothing yet",),
        output="signed the lease",
    )

    chunk = _only(remembered)
    assert chunk.metadata["milestone"] == "completed"
    assert _GOAL in chunk.text
    assert "completed" in chunk.text


@pytest.mark.asyncio
async def test_a_failed_leg_is_remembered_before_the_failure_propagates(engine: Engine) -> None:
    """A failure is what a person is most likely to ask about later, so it is recorded first.

    The continuation raises on FAILED so A0 re-delivers; the note is written at the leg
    boundary, before that, or it would never be written at all.
    """
    remembered = _FakeEpisodic()
    with pytest.raises(TaskLegFailedError):
        await _fire(
            engine,
            status=RunStatus.ERROR,
            legs_done=1,
            prior_conclusions=("nothing yet",),
            output=None,
            episodic=remembered,
        )

    chunk = _only(remembered)
    assert chunk.metadata["milestone"] == "failed"
    assert _GOAL in chunk.text
    assert "failed" in chunk.text


@pytest.mark.asyncio
async def test_a_task_parked_on_the_person_is_remembered_as_waiting(engine: Engine) -> None:
    """The milestone ``milestone_for`` defers to the wait transition, driven through it.

    A contract bound is reached at the door, so the continuation parks the task on the user,
    and that park is the ONE place every wait arrives (the approval gate and a leg that ended
    on a question come through the same method).
    """
    store, remembered, queue, runner = await _fire(
        engine, legs_done=1, bounds=ContractBounds(max_legs=1)
    )

    assert runner.runs == 0  # the bound was reached before this leg spent anything
    assert queue.enqueued == []
    parked = store.get(_OWNER, "t1")
    assert parked.state is TaskState.WAITING
    assert parked.wait_kind is WaitKind.ON_USER
    chunk = _only(remembered)
    assert chunk.metadata["milestone"] == "waiting"
    assert _GOAL in chunk.text
    assert "waiting on you" in chunk.text


@pytest.mark.asyncio
async def test_an_unwritable_memory_never_fails_the_leg(engine: Engine) -> None:
    """Memory is a side effect of the work, never a precondition for it."""
    store, remembered, queue, runner = await _fire(engine, episodic=_FakeEpisodic(broken=True))

    assert runner.runs == 1  # the leg ran
    assert remembered.chunks == []  # and remembered nothing, because it could not
    assert len(queue.enqueued) == 1  # and the task carried on regardless
    assert store.get(_OWNER, "t1").state is TaskState.ACTIVE


@pytest.mark.asyncio
async def test_a_worker_with_no_recorder_behaves_exactly_as_before(engine: Engine) -> None:
    """``None`` is the pre-wiring shape: a leg runs, and nothing is remembered of it."""
    store = TaskStore(engine)
    task = _task(store, legs_done=0, bounds=None)
    sink = _Sink()
    queue = _RecordingQueue()
    runner = _Runner(RunStatus.MAX_STEPS_REACHED, "found something")
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
    await handler.handle(
        TaskLegPayload(
            task_id="t1",
            predecessor_seq=task.head_checkpoint_seq,
            trigger=ScheduledFire(schedule_id="s", fire_time=_NOW),
        ),
        _Context(),  # type: ignore[arg-type]
    )

    assert runner.runs == 1
    assert len(queue.enqueued) == 1
