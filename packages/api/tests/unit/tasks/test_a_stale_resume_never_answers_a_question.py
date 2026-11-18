"""A Resume queued before the task asked a question never runs past it (R9-239 part 2).

The race: a pause trips a leg, and the user lifts it after the leg's checkpoint landed but
before the leg settled. Resume saw an ACTIVE task and queued a :class:`Revived` leg at the
new head. The tripped step then asked a question, so the leg parked the task waiting on the
user. When the Revived job was claimed, the handler treated it as the trigger arriving and
resumed the task, so the next leg ran without the answer.

``Revived`` is only ever minted for a task that is not waiting (Resume on a paused live
task, Raise cap, the stranded sweep), so one that finds the task waiting on the user is
stale. It is consumed; the reply carries the task on, as a reply always does. A transient
retry (:class:`AutoRetry`) is a different trigger and still picks a stuck park up.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.tasks import (
    AutoRetry,
    CheckpointKind,
    Contract,
    Revived,
    Task,
    TaskCheckpoint,
    TaskState,
    WaitKind,
    checkpoint_id,
)
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

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_OWNER = "user_stale_resume"
_PERSONA = "persona_stale_resume"
_QUESTION = "Which district should I look in?"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "stale_resume.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="s@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


class _Runner:
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
    def __init__(self, runner: _Runner) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: LegBox, *, task: object = None) -> object:  # noqa: ARG002
        return self._runner


class _Sink:
    """The checkpoint store minus its Postgres-only row id, seeded with the asking leg."""

    def __init__(self) -> None:
        self.landed: list[TaskCheckpoint] = [
            TaskCheckpoint(
                task_id="t1",
                leg_id=checkpoint_id("t1", CheckpointKind.LEG, 0),
                checkpoint_seq=0,
                open_questions=(_QUESTION,),
                updated_at=_NOW,
            )
        ]

    def get_latest(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:  # noqa: ARG002
        return self.landed[-1]

    def list_recent(self, owner_id: str, task_id: str, *, limit: int) -> list[TaskCheckpoint]:  # noqa: ARG002
        return list(reversed(self.landed))[:limit]

    def count_legs(self, owner_id: str, task_id: str) -> int:  # noqa: ARG002
        return len(self.landed)

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


class _Queue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


class _Context:
    owner_id = _OWNER
    job_id = "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:  # noqa: ARG002
        return None


async def _claim(engine: Engine, trigger: ResumeTrigger) -> tuple[TaskStore, _Runner]:
    """The task parked on its question at head 0; a job keyed at that head is claimed."""
    store = TaskStore(engine)
    store.create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="find a flat"),
            head_checkpoint_seq=0,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    store.start(_OWNER, "t1", now=_NOW)
    store.begin_wait(_OWNER, "t1", WaitKind.ON_USER, now=_NOW)
    sink = _Sink()
    runner = _Runner()
    handler = TaskLegHandler(
        task_store=store,
        checkpoint_store=sink,  # type: ignore[arg-type]
        runner_builder=_Builder(runner),  # type: ignore[arg-type]
        continuation=TaskContinuation(
            task_store=store,
            queue=_Queue(),  # type: ignore[arg-type]
            checkpoint_store=sink,  # type: ignore[arg-type]
        ),
    )
    payload = TaskLegPayload(task_id="t1", predecessor_seq=0, trigger=trigger)
    await handler.handle(payload, _Context())  # type: ignore[arg-type]
    return store, runner


@pytest.mark.asyncio
async def test_a_resume_queued_before_the_question_does_not_run_past_it(engine: Engine) -> None:
    store, runner = await _claim(engine, Revived(reason="you pressed Resume", revived_at=_NOW))

    assert runner.runs == 0
    task = store.get(_OWNER, "t1")
    assert (task.state, task.wait_kind) == (TaskState.WAITING, WaitKind.ON_USER)


@pytest.mark.asyncio
async def test_a_transient_retry_still_picks_a_stuck_park_up(engine: Engine) -> None:
    """The rule is about Revived only: the revival sweep's AutoRetry resumes a task the
    dead-leg sweep parked on the user, and must keep doing so."""
    _, runner = await _claim(engine, AutoRetry(cause="429 rate limit", retried_at=_NOW))

    assert runner.runs == 1
