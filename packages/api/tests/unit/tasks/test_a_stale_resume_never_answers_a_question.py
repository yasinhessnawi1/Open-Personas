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
from typing import TYPE_CHECKING, Any

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


# --- the reply that arrives while the stale Resume is still queued (round 2, A) -----------


def _queued_revived(engine: Engine, *, state: str = "queued") -> str:
    """Resume's Revived leg at head 0, as A0 holds it before any worker claims it."""
    from persona_api.db.models import jobs as jobs_t
    from persona_api.tasks.handler import TASK_LEG_JOB_TYPE, task_leg_idempotency_key

    payload = TaskLegPayload(
        task_id="t1",
        predecessor_seq=0,
        trigger=Revived(reason="you pressed Resume", revived_at=_NOW),
    )
    key = task_leg_idempotency_key(payload)
    with engine.begin() as conn:
        conn.execute(
            insert(jobs_t).values(
                id="job-revived",
                type=TASK_LEG_JOB_TYPE,
                owner_id=_OWNER,
                payload=payload.model_dump(mode="json"),
                idempotency_key=key,
                state=state,
                scheduled_at=_NOW,
                created_at=_NOW,
            )
        )
    return key


def _job_state(engine: Engine, job_id: str) -> str:
    from persona_api.db.models import jobs as jobs_t
    from sqlalchemy import select

    with engine.begin() as conn:
        return str(conn.execute(select(jobs_t.c.state).where(jobs_t.c.id == job_id)).scalar())


def _parked_on_its_question(engine: Engine) -> Task:
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
    return store.begin_wait(_OWNER, "t1", WaitKind.ON_USER, now=_NOW)


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The legs queued, as ``"{key} {trigger kind}"`` (A0's enqueue is Postgres-only)."""
    from persona_api.jobs.queue import JobQueue

    keys: list[str] = []

    def _enqueue(self: JobQueue, **kwargs: object) -> None:  # noqa: ARG001
        trigger = dict(kwargs["payload"])["trigger"]  # type: ignore[call-overload]
        keys.append(f"{kwargs['idempotency_key']} {trigger['kind']}")

    monkeypatch.setattr(JobQueue, "enqueue", _enqueue)
    return keys


def test_a_reply_is_never_absorbed_by_the_stale_resume_still_queued(
    engine: Engine, enqueued: list[str]
) -> None:
    """Resume queued Revived at ``after:0`` while the tripped leg was still settling; the leg
    then parked on its question, and the user answered before Revived was claimed. The reply's
    leg took the same key, A0 dropped it as a duplicate, and the claim then consumed Revived:
    the answer was lost while the user was told it went through. The stale Resume is retired
    and the reply gets a key of its own, so exactly one leg runs, carrying the reply."""
    from persona_api.services import task_control_service

    stale_key = _queued_revived(engine)
    task = _parked_on_its_question(engine)

    outcome = task_control_service.reply_to_task(
        engine, _OWNER, task, "Grunerlokka, please.", now=_NOW
    )

    assert outcome.changed is True
    assert _job_state(engine, "job-revived") != "queued"  # it will never be claimed
    assert enqueued == [f"{stale_key}:retry:1 user_reply"]  # not the stale key


def test_a_stale_resume_already_claimed_still_leaves_the_reply_its_own_key(
    engine: Engine, enqueued: list[str]
) -> None:
    """Claimed but not yet run: it cannot be retired, and its guard will consume it, so it is
    counted as spent and the reply still does not collide with it."""
    from persona_api.services import task_control_service

    stale_key = _queued_revived(engine, state="claimed")
    task = _parked_on_its_question(engine)

    task_control_service.reply_to_task(engine, _OWNER, task, "Grunerlokka, please.", now=_NOW)

    assert _job_state(engine, "job-revived") == "claimed"
    assert enqueued == [f"{stale_key}:retry:1 user_reply"]


def test_a_claimed_resume_finishing_between_the_two_counts_still_leaves_the_reply_its_key(
    engine: Engine, enqueued: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's N1: the stale Revived is running when the reply counts, and its guard
    consumes it (succeeded) between the two counts. Counted spent first, it was still live;
    counted live second, it had finished: neither saw it, and the reply took its key. Whichever
    count runs first, the job finishes right after it, and the reply must still miss it."""
    from persona_api.db.models import jobs as jobs_t
    from persona_api.jobs.queue import JobQueue
    from persona_api.services import task_control_service
    from sqlalchemy import update

    stale_key = _queued_revived(engine, state="running")
    task = _parked_on_its_question(engine)
    finished = False

    def _finishes_after(original: Any) -> Any:  # noqa: ANN401
        def _count(self: JobQueue, **kwargs: object) -> int:
            nonlocal finished
            counted = int(original(self, **kwargs))
            if not finished:
                finished = True
                with engine.begin() as conn:
                    conn.execute(
                        update(jobs_t).where(jobs_t.c.id == "job-revived").values(state="succeeded")
                    )
            return counted

        return _count

    monkeypatch.setattr(
        JobQueue, "count_spent_attempts", _finishes_after(JobQueue.count_spent_attempts)
    )
    monkeypatch.setattr(
        JobQueue, "count_live_attempts", _finishes_after(JobQueue.count_live_attempts)
    )

    task_control_service.reply_to_task(engine, _OWNER, task, "Grunerlokka, please.", now=_NOW)

    assert len(enqueued) == 1
    assert not enqueued[0].startswith(f"{stale_key} ")  # never the stale job's own key


# --- the queue's retire and live count touch only what they name (round 3) -----------------


def _job(engine: Engine, job_id: str, *, task_id: str, kind: str, state: str, key: str) -> None:
    from persona.tasks import UserReply
    from persona_api.db.models import jobs as jobs_t
    from persona_api.tasks.handler import TASK_LEG_JOB_TYPE

    trigger = (
        Revived(reason="you pressed Resume", revived_at=_NOW)
        if kind == "revived"
        else UserReply(reply="an earlier answer")
    )
    payload = TaskLegPayload(task_id=task_id, predecessor_seq=0, trigger=trigger)
    with engine.begin() as conn:
        conn.execute(
            insert(jobs_t).values(
                id=job_id,
                type=TASK_LEG_JOB_TYPE,
                owner_id=_OWNER,
                payload=payload.model_dump(mode="json"),
                idempotency_key=key,
                state=state,
                scheduled_at=_NOW,
                created_at=_NOW,
            )
        )


@pytest.mark.usefixtures("enqueued")
def test_a_reply_retires_only_its_own_tasks_queued_resumes(engine: Engine) -> None:
    """Another task's queued Revived, and this task's queued leg of another kind, are not
    the stale Resume: both stay queued."""
    from persona_api.services import task_control_service

    _queued_revived(engine)
    task = _parked_on_its_question(engine)
    _job(
        engine,
        "job-other-task",
        task_id="t2",
        kind="revived",
        state="queued",
        key="task:t2:after:0",
    )
    _job(
        engine,
        "job-other-kind",
        task_id="t1",
        kind="user_reply",
        state="queued",
        key="task:t1:after:7",
    )

    task_control_service.reply_to_task(engine, _OWNER, task, "Grunerlokka, please.", now=_NOW)

    assert _job_state(engine, "job-revived") == "succeeded"
    assert _job_state(engine, "job-other-task") == "queued"
    assert _job_state(engine, "job-other-kind") == "queued"


def test_a_claimed_resume_at_a_retry_key_is_counted_too(
    engine: Engine, enqueued: list[str]
) -> None:
    """A Resume that itself had to move past a spent attempt sits at ``after:0:retry:1``. A
    worker claimed it; the reply must count it and move past it as well."""
    from persona_api.services import task_control_service

    task = _parked_on_its_question(engine)
    _job(
        engine,
        "job-spent",
        task_id="t1",
        kind="revived",
        state="succeeded",
        key="task:t1:after:0",
    )
    _job(
        engine,
        "job-claimed-retry",
        task_id="t1",
        kind="revived",
        state="claimed",
        key="task:t1:after:0:retry:1",
    )

    task_control_service.reply_to_task(engine, _OWNER, task, "Grunerlokka, please.", now=_NOW)

    assert enqueued == ["task:t1:after:0:retry:2 user_reply"]
