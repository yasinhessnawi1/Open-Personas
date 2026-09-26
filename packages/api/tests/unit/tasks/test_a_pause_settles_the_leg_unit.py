"""A leg that ends under a pause settles what it finished and holds what is left (R9-158).

The control watcher (D-W1-21) stops a running leg at its next step boundary when the task
is paused. The step that boundary opens still runs, and it can finish the work. The handler
used to discard every outcome of a tripped leg, so a task whose last leg completed stayed
ACTIVE and paused, with Resume offered on finished work. The owner ruled on 2026-09-26
that a pause pressed while a leg finishes the work completes the task.

The rule these pin, on the community engine with the real ``TaskStore`` and the real
``TaskContinuation`` (the queue records what would be enqueued; A0's enqueue is
Postgres-only, and the chain version of each case is in
``tests/integration/test_a_pause_settles_the_leg.py``):

- COMPLETED completes and WAITING_USER parks on the question, pause or no pause;
- CONTINUE's next leg, FAILED's retry and an approval park are held until Resume, which
  re-enqueues from the head (D-W1-30), whether the pause tripped the leg or landed after
  its last boundary;
- a pause lifted while the leg still ran does not get a second next leg from the handler.

Pause is pressed through ``task_control_service.pause_task``, the one pause every door uses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.errors import GatedActionProposedError, TaskLegFailedError
from persona.tasks import (
    Contract,
    IntrospectionStatus,
    ScheduledFire,
    Task,
    TaskState,
    WaitKind,
    summarise_task,
)
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.db.models import tasks as tasks_t
from persona_api.services import task_control_service
from persona_api.tasks import TaskLegHandler, TaskLegPayload, TaskStore
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.store import CheckpointStore
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from sqlalchemy import insert, update

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping
    from pathlib import Path

    from persona.tasks import LegBox, SpendKind, TaskCheckpoint
    from persona_runtime.agentic.run import CancelToken, StepUsage
    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
_OWNER = "user_pause_settles"
_PERSONA = "persona_pause_settles"
_QUESTION = "Which account should I use?"

#: When the pause is pressed, relative to the leg's last step boundary.
_BEFORE_LAST_BOUNDARY = "before"  # the watcher reads it there and trips the leg
_AFTER_LAST_BOUNDARY = "after"  # no boundary is left to read it; nothing trips


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "pause_settles.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="s@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine: Engine) -> TaskStore:
    s = TaskStore(engine)
    s.create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="find three sources"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    s.start(_OWNER, "t1", now=_NOW)
    return s


class _DedupQueue:
    """A0's enqueue contract without Postgres: a key already held is absorbed.

    ``INSERT ... ON CONFLICT (owner_id, idempotency_key) DO NOTHING``: the leg under test is
    itself a live job keyed ``task:t1:after:init``, so an enqueue at that key (a Resume
    pressed while it runs, before its salvage lands) is absorbed, exactly as in production.
    ``keys`` is what was newly queued, in order.
    """

    def __init__(self) -> None:
        self._held = {"task:t1:after:init"}  # the running leg's own job
        self.keys: list[str] = []

    def count_spent_attempts(self, *, owner_id: str, idempotency_key: str) -> int:  # noqa: ARG002
        return 0

    def enqueue(self, **kwargs: object) -> None:
        key = str(kwargs["idempotency_key"])
        if key in self._held:
            return
        self._held.add(key)
        self.keys.append(key)


def _run(task: str, status: RunStatus) -> Run:
    steps = [Step(type=StepType.REASONING, content="read the first source", tokens=10)]
    if status is RunStatus.AWAITING_USER:
        steps.append(Step(type=StepType.ASK_USER, question=_QUESTION, tokens=5))
    return Run(
        persona_id=_PERSONA,
        task=task,
        status=status,
        steps=steps,
        output=None if status is RunStatus.AWAITING_USER else "one source read so far",
        error="the provider timed out" if status is RunStatus.ERROR else None,
        started_at=_NOW,
        finished_at=_NOW,
    )


class _PausedLeg:
    """A two-step leg; the user presses Pause from another surface while it runs.

    ``press`` says where: before the leg's last step boundary (the control watcher reads the
    row there and trips the leg) or after it (nothing is left to read it). The step after
    the last boundary still runs and ends the leg as ``ends``. ``lift`` presses Resume after
    the trip, while the leg is still running. ``gate`` ends the leg on a gated action instead.
    """

    def __init__(
        self,
        engine: Engine,
        store: TaskStore,
        *,
        ends: RunStatus,
        press: str | None,
        lift_with: _DedupQueue | None = None,
        gate: bool = False,
    ) -> None:
        self._engine = engine
        self._store = store
        self._ends = ends
        self._press = press
        self._lift_with = lift_with
        self._gate = gate
        self.tripped_at_last_boundary = False

    def _pause(self) -> None:
        task = self._store.get(_OWNER, "t1")
        task_control_service.pause_task(self._engine, _OWNER, task, now=_NOW)

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,  # noqa: ARG002
    ) -> Run:
        await on_event(RunEvent.thinking(0))
        if self._press == _BEFORE_LAST_BOUNDARY:
            self._pause()
        await on_event(RunEvent.thinking(1))  # the last boundary: the step after it still runs
        self.tripped_at_last_boundary = cancel_token.is_cancelled
        if self._press == _AFTER_LAST_BOUNDARY:
            self._pause()
        if self._lift_with is not None:
            task_control_service.resume_task(
                self._engine,
                _OWNER,
                self._store.get(_OWNER, "t1"),
                now=_NOW,
                queue=self._lift_with,  # type: ignore[arg-type]
            )
        if self._gate:
            raise GatedActionProposedError(
                "gated", context={"proposal_id": "p1", "description": "send the summary email"}
            )
        return _run(task, self._ends)


class _Builder:
    def __init__(self, runner: object) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: LegBox, *, task: object = None) -> object:  # noqa: ARG002
        return self._runner


class _Sink:
    """Lands the checkpoint: the durable head moves as the real CAS append moves it.

    ``after_append`` runs once the head has moved, for a control pressed between the
    salvage landing and the handler settling the leg.
    """

    def __init__(self, engine: Engine, after_append: Callable[[], None] | None = None) -> None:
        self._engine = engine
        self._after_append = after_append

    def get_latest(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:  # noqa: ARG002
        return None

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int] | None = None,  # noqa: ARG002
        now: datetime,
    ) -> Task:
        with self._engine.begin() as conn:
            conn.execute(
                update(tasks_t)
                .where(tasks_t.c.id == task.id)
                .values(head_checkpoint_seq=checkpoint.checkpoint_seq)
            )
        if self._after_append is not None:
            self._after_append()
        return task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)


class _Context:
    owner_id = _OWNER
    job_id = "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:  # noqa: ARG002
        return None


async def _run_the_leg(
    engine: Engine,
    store: TaskStore,
    leg: _PausedLeg,
    queue: _DedupQueue,
    *,
    after_append: Callable[[], None] | None = None,
) -> None:
    handler = TaskLegHandler(
        task_store=store,
        checkpoint_store=_Sink(engine, after_append),  # type: ignore[arg-type]
        runner_builder=_Builder(leg),  # type: ignore[arg-type]
        continuation=TaskContinuation(
            task_store=store,
            queue=queue,  # type: ignore[arg-type]
            checkpoint_store=CheckpointStore(engine),
        ),
    )
    payload = TaskLegPayload(
        task_id="t1", predecessor_seq=None, trigger=ScheduledFire(schedule_id="s", fire_time=_NOW)
    )
    await handler.handle(payload, _Context())  # type: ignore[arg-type]


def _truth(store: TaskStore) -> tuple[TaskState, bool, WaitKind | None, IntrospectionStatus]:
    """The durable row as the task page reads it."""
    task = store.get(_OWNER, "t1")
    return (task.state, task.paused, task.wait_kind, summarise_task(task).status)


# --- outcomes that end the leg's work settle, pause or no pause ---------------------------


@pytest.mark.asyncio
async def test_a_leg_that_finishes_the_work_after_a_pause_tripped_it_completes_the_task(
    engine: Engine, store: TaskStore
) -> None:
    queue = _DedupQueue()
    leg = _PausedLeg(engine, store, ends=RunStatus.COMPLETED, press=_BEFORE_LAST_BOUNDARY)
    await _run_the_leg(engine, store, leg, queue)
    assert leg.tripped_at_last_boundary
    assert (_truth(store), queue.keys) == (
        (TaskState.COMPLETED, False, None, IntrospectionStatus.COMPLETED),
        [],
    )


@pytest.mark.asyncio
async def test_a_leg_that_asks_after_a_pause_tripped_it_parks_the_task_on_the_question(
    engine: Engine, store: TaskStore
) -> None:
    queue = _DedupQueue()
    leg = _PausedLeg(engine, store, ends=RunStatus.AWAITING_USER, press=_BEFORE_LAST_BOUNDARY)
    await _run_the_leg(engine, store, leg, queue)
    assert leg.tripped_at_last_boundary
    # Parked on the question AND still paused: the reply is refused while paused (D-W1-10),
    # so the question waits for Resume, which only clears a waiting task's overlay.
    assert (_truth(store), queue.keys) == (
        (TaskState.WAITING, True, WaitKind.ON_USER, IntrospectionStatus.PAUSED),
        [],
    )


# --- anything that would put more work on the queue is held until Resume ------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("press", [_BEFORE_LAST_BOUNDARY, _AFTER_LAST_BOUNDARY])
async def test_a_leg_that_would_continue_under_a_pause_holds_its_next_leg(
    engine: Engine, store: TaskStore, press: str
) -> None:
    # BEFORE: the pause tripped the leg (CANCELLED). AFTER: the pause landed once no
    # boundary was left, and the leg ran out of steps on its own; nothing tripped it, but
    # enqueueing its next leg would only make a job the claim consumes (and, with the cap
    # reached, the budget gate would try to pause an already-paused task and raise).
    ends = RunStatus.CANCELLED if press == _BEFORE_LAST_BOUNDARY else RunStatus.MAX_STEPS_REACHED
    queue = _DedupQueue()
    leg = _PausedLeg(engine, store, ends=ends, press=press)
    await _run_the_leg(engine, store, leg, queue)
    assert leg.tripped_at_last_boundary is (press == _BEFORE_LAST_BOUNDARY)
    assert (_truth(store), queue.keys) == (
        (TaskState.ACTIVE, True, None, IntrospectionStatus.PAUSED),
        [],
    )


@pytest.mark.asyncio
async def test_a_leg_that_failed_under_a_pause_holds_its_retry(
    engine: Engine, store: TaskStore
) -> None:
    # Without a pause a FAILED leg raises so A0 retries it. Under a pause the retry would be
    # consumed at claim; the leg is held, and Resume re-enqueues from the head.
    queue = _DedupQueue()
    leg = _PausedLeg(engine, store, ends=RunStatus.ERROR, press=_BEFORE_LAST_BOUNDARY)
    await _run_the_leg(engine, store, leg, queue)
    assert (_truth(store), queue.keys) == (
        (TaskState.ACTIVE, True, None, IntrospectionStatus.PAUSED),
        [],
    )


@pytest.mark.asyncio
async def test_a_leg_that_hit_an_approval_under_a_pause_stays_held_for_resume(
    engine: Engine, store: TaskStore
) -> None:
    # Parking it waiting(on_user) would strand it: an approval answered in the inbox while
    # paused enqueues a leg the claim consumes, and Resume only clears a waiting task's
    # overlay. Held ACTIVE, Resume re-enqueues from the head.
    queue = _DedupQueue()
    leg = _PausedLeg(
        engine, store, ends=RunStatus.CANCELLED, press=_BEFORE_LAST_BOUNDARY, gate=True
    )
    await _run_the_leg(engine, store, leg, queue)
    assert (_truth(store), queue.keys) == (
        (TaskState.ACTIVE, True, None, IntrospectionStatus.PAUSED),
        [],
    )


@pytest.mark.asyncio
async def test_a_pause_lifted_before_the_salvage_lands_leaves_exactly_one_live_leg(
    engine: Engine, store: TaskStore
) -> None:
    # Pause trips the leg, then Resume lands while it still runs. Resume keys the old head,
    # which this leg's own live job holds, so A0 absorbs it. The leg, no longer paused,
    # continues from its salvage: exactly one leg is left to run, not none.
    queue = _DedupQueue()
    leg = _PausedLeg(
        engine, store, ends=RunStatus.CANCELLED, press=_BEFORE_LAST_BOUNDARY, lift_with=queue
    )
    await _run_the_leg(engine, store, leg, queue)
    assert leg.tripped_at_last_boundary
    assert (_truth(store), queue.keys) == (
        (TaskState.ACTIVE, False, None, IntrospectionStatus.PROGRESSING),
        ["task:t1:after:0"],
    )


@pytest.mark.asyncio
async def test_a_pause_lifted_after_the_salvage_lands_leaves_exactly_one_live_leg(
    engine: Engine, store: TaskStore
) -> None:
    # Pause trips the leg; Resume lands after the salvage moved the head. Resume keys the new
    # head, and the leg's own continuation dedups onto it: still exactly one.
    queue = _DedupQueue()
    leg = _PausedLeg(engine, store, ends=RunStatus.CANCELLED, press=_BEFORE_LAST_BOUNDARY)

    def resume_after_the_append() -> None:
        task_control_service.resume_task(
            engine,
            _OWNER,
            store.get(_OWNER, "t1"),
            now=_NOW,
            queue=queue,  # type: ignore[arg-type]
        )

    await _run_the_leg(engine, store, leg, queue, after_append=resume_after_the_append)
    assert leg.tripped_at_last_boundary
    assert (_truth(store), queue.keys) == (
        (TaskState.ACTIVE, False, None, IntrospectionStatus.PROGRESSING),
        ["task:t1:after:0"],
    )


# --- and with no pause at all, nothing changed -------------------------------------------


@pytest.mark.asyncio
async def test_without_a_pause_a_continuing_leg_enqueues_its_next_leg(
    engine: Engine, store: TaskStore
) -> None:
    queue = _DedupQueue()
    leg = _PausedLeg(engine, store, ends=RunStatus.MAX_STEPS_REACHED, press=None)
    await _run_the_leg(engine, store, leg, queue)
    assert (_truth(store), queue.keys) == (
        (TaskState.ACTIVE, False, None, IntrospectionStatus.PROGRESSING),
        ["task:t1:after:0"],
    )


@pytest.mark.asyncio
async def test_without_a_pause_a_failed_leg_still_raises_for_a0_to_retry(
    engine: Engine, store: TaskStore
) -> None:
    leg = _PausedLeg(engine, store, ends=RunStatus.ERROR, press=None)
    with pytest.raises(TaskLegFailedError):
        await _run_the_leg(engine, store, leg, _DedupQueue())
