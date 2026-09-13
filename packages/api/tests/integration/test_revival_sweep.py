"""The revival sweep puts back work that stopped for a reason that has passed (Spec W1, T8).

Every chain here drives the REAL sweep against real rows: no hand-set end state, and the
revival always goes through the real continuation, so what it enqueues is a job the real
worker can claim.

- **R9-148, the acceptance the owner named.** Pause owner autonomy, let the worker CONSUME the
  queued leg (the kill-switch guard skips it and the job succeeds without running), resume
  autonomy, and find the task running again from its head. Nothing else rescues this: the job
  is spent, the task is `active`, and D-W1-30's resume seam only covers the door the user
  pressed.
- **D-W1-8, transient only, once, and gated.** A leg that dead-lettered on a rate limit is
  picked up on the user's behalf after its grace window; one that dead-lettered over budget is
  offered and never taken; a second tick does not pick the same park up twice; a paused owner
  or a suspended persona stops both halves; and a newer task with the same goal supersedes the
  old one (D-W1-9).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.tasks import TRANSIENT_RETRY_AFTER, Contract, Task, TaskKind, TaskState, WaitKind
from persona_api.approvals.kill_switch import KillSwitchStore
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import enqueue_task_leg
from persona_api.tasks.revival_sweep import (
    REVIVAL_AUDIT_ACTION,
    STRANDED_GRACE,
    RevivalSweeper,
    normalise_goal,
)
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_UID = "user_w1_revival"
_PERSONA = "persona_w1_revival"
_NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


class _AlwaysLeader:
    """The leadership gate, held. Leadership itself is A1's, tested there."""

    def try_become_leader(self) -> bool:
        return True


class _NeverLeader:
    def try_become_leader(self) -> bool:
        return False


@pytest.fixture
def su_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ordering
    """The worker's cross-tenant engine (the candidate scan reads every tenant's rows)."""
    engine = make_rls_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _UID, "e": f"{_UID}@x"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y) "
                "ON CONFLICT DO NOTHING"
            ),
            {"i": _PERSONA, "o": _UID, "y": "name: Astrid"},
        )
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": _UID})
    engine.dispose()


@pytest.fixture
def app_engine(su_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ordering
    """The owner-scoped engine, with this owner's scope bound (as the sweep binds it)."""
    from persona_api.middleware.rls_context import current_user_id

    engine = make_rls_engine(os.environ["APP_DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    token = current_user_id.set(_UID)
    yield engine
    current_user_id.reset(token)
    engine.dispose()


def _sweeper(
    app_engine: Engine, su_engine: Engine, *, leader: object | None = None
) -> RevivalSweeper:
    return RevivalSweeper(
        continuation=TaskContinuation(
            task_store=TaskStore(app_engine),
            queue=JobQueue(app_engine),
            checkpoint_store=CheckpointStore(app_engine),
        ),
        dispatch_engine=su_engine,
        rls_engine=app_engine,
        task_store=TaskStore(app_engine),
        kill_switch=KillSwitchStore(app_engine),
        leader=leader or _AlwaysLeader(),  # type: ignore[arg-type]
    )


def _task(app_engine: Engine, task_id: str, goal: str, *, at: datetime) -> Task:
    store = TaskStore(app_engine)
    store.create(
        Task(
            id=task_id,
            owner_id=_UID,
            persona_id=_PERSONA,
            contract=Contract(goal=goal),
            kind=TaskKind.AD_HOC,
            created_at=at,
            updated_at=at,
        )
    )
    return store.start(_UID, task_id, now=at)


def _age(su_engine: Engine, task_id: str, *, by: timedelta) -> None:
    """Backdate the task's ``updated_at`` so the grace window has genuinely elapsed.

    The windows are minutes long by design (a leg that just finished must not be revived under
    it), so the clock is moved rather than the test sleeping.
    """
    with su_engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET updated_at = updated_at - CAST(:iv AS INTERVAL) WHERE id = :t"),
            {"iv": f"{int(by.total_seconds())} seconds", "t": task_id},
        )


def _jobs(su_engine: Engine, task_id: str) -> list[tuple[str, str]]:
    with su_engine.begin() as conn:
        return [
            (str(r[0]), str(r[1]))
            for r in conn.execute(
                text(
                    "SELECT state, idempotency_key FROM jobs WHERE type = 'task_leg' "
                    "AND payload->>'task_id' = :t ORDER BY created_at"
                ),
                {"t": task_id},
            )
        ]


def _dead_letter(su_engine: Engine, task_id: str, cause: str) -> None:
    """Turn the task's queued leg into a REAL dead letter, the way A0 does."""
    queue = JobQueue(su_engine)
    claimed = queue.claim(worker_id="w-revival", lease_seconds=60, limit=20)
    job = next(j for j in claimed if j.payload.get("task_id") == task_id)
    for other in claimed:
        if other.id != job.id:
            queue.retry(job_id=other.id, worker_id="w-revival", error="not mine", scheduled_at=_NOW)
    assert queue.mark_running(job_id=job.id, worker_id="w-revival")
    assert queue.mark_dead(job_id=job.id, worker_id="w-revival", error=cause)


def _park_on_the_dead_leg(app_engine: Engine, su_engine: Engine, task_id: str) -> None:
    """The REAL dead-leg reaction: the task parks waiting(on_user) with the cause."""
    continuation = TaskContinuation(
        task_store=TaskStore(app_engine),
        queue=JobQueue(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
    )
    assert continuation.sweep_dead_legs(JobQueue(su_engine), now=datetime.now(UTC)) >= 1
    parked = TaskStore(app_engine).get(_UID, task_id)
    assert parked.state is TaskState.WAITING
    assert parked.wait_kind is WaitKind.ON_USER


# --- R9-148: a leg consumed without running leaves the task alive and stuck -------------------


@pytest.mark.asyncio
async def test_autonomy_paused_then_resumed_leaves_the_task_running_again(
    app_engine: Engine, su_engine: Engine
) -> None:
    """The owner's acceptance, end to end and with nothing hand-forced.

    The kill-switch guard CONSUMES the leg while autonomy is paused: the job succeeds without
    running and the task stays `active` with nothing queued. Resuming autonomy does not by
    itself put the work back — that is R9-148 — so the sweep does.
    """
    from persona.jobs import JobRegistry
    from persona_api.tasks.handler import register_task_leg_handler

    task = _task(app_engine, "t_stranded", "brief hacker news", at=datetime.now(UTC))
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id=_UID,
        task_id=task.id,
        predecessor_seq=None,
        trigger=_fire(),
    )
    kill_switch = KillSwitchStore(app_engine)
    kill_switch.pause_owner(_UID, actor="user", now=datetime.now(UTC))

    # The REAL claim-side guard consumes the leg: no leg runs, and the job is spent.
    registry = JobRegistry()
    register_task_leg_handler(
        registry,
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_NoRunnerBuilder(),  # type: ignore[arg-type]
        runnable_guard=kill_switch,
        rls_engine=app_engine,
    )
    from persona_api.jobs import Worker

    worker = Worker(
        dispatch_engine=su_engine,
        rls_engine=app_engine,
        registry=registry,
        worker_id="w-revival-guard",
    )
    assert await worker.run_once() == 1
    assert [state for state, _ in _jobs(su_engine, task.id)] == ["succeeded"]
    assert TaskStore(app_engine).get(_UID, task.id).state is TaskState.ACTIVE  # alive, stuck

    # Resuming autonomy alone changes nothing: there is no job to run (R9-148).
    kill_switch.resume_owner(_UID, now=datetime.now(UTC))
    assert [state for state, _ in _jobs(su_engine, task.id)] == ["succeeded"]

    _age(su_engine, task.id, by=STRANDED_GRACE + timedelta(minutes=1))
    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 1

    states = _jobs(su_engine, task.id)
    assert [s for s, _ in states] == ["succeeded", "queued"], states
    # Keyed past the spent row at the same head (D-W1-29), so the duplicate guard cannot eat it.
    assert states[1][1] == f"task:{task.id}:after:init:retry:1"

    # ...and the last hop is DRIVEN, never assumed (the A4 lesson). A queued job proves the
    # sweep enqueued something; only running it proves the payload it wrote can be read back
    # and executed — the revived trigger included, since the handler deserialises it.
    runner = _RecordingRunner()
    ran = Worker(
        dispatch_engine=su_engine,
        rls_engine=app_engine,
        registry=_registry(app_engine, runner),
        worker_id="w-revival-run",
    )
    assert await ran.run_once() == 1
    assert runner.calls == 1, "the revived leg actually ran"
    assert [s for s, _ in _jobs(su_engine, task.id)] == ["succeeded", "succeeded"]
    revived = TaskStore(app_engine).get(_UID, task.id)
    assert revived.head_checkpoint_seq == 0  # the leg's checkpoint landed: real work happened


@pytest.mark.asyncio
async def test_a_task_with_a_live_job_is_left_alone(app_engine: Engine, su_engine: Engine) -> None:
    """The sweep is for work with NOTHING running. A queued leg is not stranded, and a second
    enqueue would run the same leg twice."""
    task = _task(app_engine, "t_live", "summarise the news", at=datetime.now(UTC))
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id=_UID,
        task_id=task.id,
        predecessor_seq=None,
        trigger=_fire(),
    )
    _age(su_engine, task.id, by=STRANDED_GRACE + timedelta(minutes=1))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert [s for s, _ in _jobs(su_engine, task.id)] == ["queued"]


@pytest.mark.asyncio
async def test_a_task_that_only_just_stopped_is_left_alone(
    app_engine: Engine, su_engine: Engine
) -> None:
    """The grace window: between a leg's checkpoint landing and its continuation being
    enqueued a task legitimately has no live job, and reviving there runs the leg twice."""
    task = _task(app_engine, "t_fresh", "book the dentist", at=datetime.now(UTC))
    assert _jobs(su_engine, task.id) == []  # alive with nothing queued, but only just now

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert _jobs(su_engine, task.id) == []


@pytest.mark.asyncio
async def test_a_follower_does_nothing(app_engine: Engine, su_engine: Engine) -> None:
    task = _task(app_engine, "t_follower", "brief hacker news", at=datetime.now(UTC))
    _age(su_engine, task.id, by=STRANDED_GRACE + timedelta(minutes=1))

    swept = await _sweeper(app_engine, su_engine, leader=_NeverLeader()).run_once(
        now=datetime.now(UTC)
    )
    assert swept == 0
    assert _jobs(su_engine, task.id) == []


# --- D-W1-8: a transient failure, picked up once ----------------------------------------------


@pytest.mark.asyncio
async def test_a_transient_dead_letter_is_picked_up_once_on_the_users_behalf(
    app_engine: Engine, su_engine: Engine
) -> None:
    task = _task(app_engine, "t_transient", "brief hacker news", at=datetime.now(UTC))
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id=_UID,
        task_id=task.id,
        predecessor_seq=None,
        trigger=_fire(),
    )
    _dead_letter(su_engine, task.id, "rate limit exceeded on every provider")
    _park_on_the_dead_leg(app_engine, su_engine, task.id)
    _age(su_engine, task.id, by=TRANSIENT_RETRY_AFTER + timedelta(minutes=1))

    sweeper = _sweeper(app_engine, su_engine)
    assert await sweeper.run_once(now=datetime.now(UTC)) == 1
    states = _jobs(su_engine, task.id)
    assert [s for s, _ in states] == ["dead", "queued"], states
    assert states[1][1] == f"task:{task.id}:after:init:retry:1"  # past the dead row

    # ONCE per park: a second tick does not pick the same one up again.
    _age(su_engine, task.id, by=TRANSIENT_RETRY_AFTER + timedelta(minutes=1))
    assert await sweeper.run_once(now=datetime.now(UTC)) == 0
    assert len(_jobs(su_engine, task.id)) == 2

    with su_engine.begin() as conn:
        marks = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = :a AND target = :t"),
            {"a": REVIVAL_AUDIT_ACTION, "t": task.id},
        ).scalar_one()
    assert marks == 1  # the once-marker, written before the resume


@pytest.mark.asyncio
async def test_a_deterministic_dead_letter_is_offered_and_never_taken(
    app_engine: Engine, su_engine: Engine
) -> None:
    """Over budget is not the world being briefly unavailable: trying again spends the owner's
    credits on work that cannot succeed, so it stays an offer."""
    task = _task(app_engine, "t_deterministic", "research every flight", at=datetime.now(UTC))
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id=_UID,
        task_id=task.id,
        predecessor_seq=None,
        trigger=_fire(),
    )
    _dead_letter(su_engine, task.id, "task budget exhausted; refusing to overrun it")
    _park_on_the_dead_leg(app_engine, su_engine, task.id)
    _age(su_engine, task.id, by=TRANSIENT_RETRY_AFTER + timedelta(minutes=1))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert [s for s, _ in _jobs(su_engine, task.id)] == ["dead"]
    assert TaskStore(app_engine).get(_UID, task.id).state is TaskState.WAITING  # still offered


@pytest.mark.asyncio
async def test_a_transient_park_inside_its_grace_window_is_left_alone(
    app_engine: Engine, su_engine: Engine
) -> None:
    """The user gets first refusal: nothing is picked up on their behalf until the window
    named in the offer has actually passed."""
    task = _task(app_engine, "t_too_soon", "brief hacker news", at=datetime.now(UTC))
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id=_UID,
        task_id=task.id,
        predecessor_seq=None,
        trigger=_fire(),
    )
    _dead_letter(su_engine, task.id, "429 rate limit")
    _park_on_the_dead_leg(app_engine, su_engine, task.id)

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert [s for s, _ in _jobs(su_engine, task.id)] == ["dead"]


@pytest.mark.asyncio
async def test_a_paused_owner_stops_every_revival(app_engine: Engine, su_engine: Engine) -> None:
    """The pause is the whole point: nothing runs on the owner's behalf while it holds."""
    stranded = _task(app_engine, "t_paused_stranded", "brief hacker news", at=datetime.now(UTC))
    _age(su_engine, stranded.id, by=STRANDED_GRACE + timedelta(minutes=1))
    KillSwitchStore(app_engine).pause_owner(_UID, actor="user", now=datetime.now(UTC))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert _jobs(su_engine, stranded.id) == []

    KillSwitchStore(app_engine).resume_owner(_UID, now=datetime.now(UTC))


@pytest.mark.asyncio
async def test_a_suspended_persona_stops_its_tasks_being_revived(
    app_engine: Engine, su_engine: Engine
) -> None:
    task = _task(app_engine, "t_suspended", "brief hacker news", at=datetime.now(UTC))
    _age(su_engine, task.id, by=STRANDED_GRACE + timedelta(minutes=1))
    kill_switch = KillSwitchStore(app_engine)
    kill_switch.suspend_persona(_UID, _PERSONA, now=datetime.now(UTC))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert _jobs(su_engine, task.id) == []


@pytest.mark.asyncio
async def test_a_newer_task_with_the_same_goal_supersedes_the_old_one(
    app_engine: Engine, su_engine: Engine
) -> None:
    """D-W1-9: the user asking for the same thing again is the clearest signal that the old
    attempt is not wanted. Reviving it would do the work twice and bill for both."""
    old = _task(
        app_engine, "t_old", "Brief   Hacker News", at=datetime.now(UTC) - timedelta(hours=2)
    )
    _age(su_engine, old.id, by=STRANDED_GRACE + timedelta(minutes=1))
    _task(app_engine, "t_new", "brief hacker news", at=datetime.now(UTC))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert _jobs(su_engine, old.id) == []


def test_goals_are_compared_after_normalising() -> None:
    assert normalise_goal("Brief   Hacker News") == normalise_goal("brief hacker news")
    assert normalise_goal("book the dentist") != normalise_goal("book the doctor")


# --- helpers ---------------------------------------------------------------------------------


def _fire() -> object:
    from persona.tasks import ScheduledFire

    return ScheduledFire(schedule_id="s1", fire_time=_NOW)


class _RecordingRunner:
    """A leg that finishes, so the revived job runs all the way through the real handler."""

    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        task: str,
        *,
        on_event: object,
        cancel_token: object,  # noqa: ARG002
        on_step_usage: object = None,  # noqa: ARG002
    ) -> object:
        from persona_runtime.agentic.events import RunEvent
        from persona_runtime.agentic.run import Run, RunStatus
        from persona_runtime.agentic.step import Step, StepType

        self.calls += 1
        await on_event(RunEvent.thinking(0))  # type: ignore[operator]
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.COMPLETED,
            steps=[Step(type=StepType.FINAL, content="done", tokens=10)],
            output="done",
            started_at=_NOW,
            finished_at=_NOW,
        )


class _OneRunnerBuilder:
    """Hands the same runner to every leg (the A2 builder Protocol)."""

    def __init__(self, runner: object) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: object, *, task: object = None) -> object:  # noqa: ARG002
        return self._runner


def _registry(app_engine: Engine, runner: object) -> object:
    """A job registry with the REAL task-leg handler over ``runner``."""
    from persona.jobs import JobRegistry
    from persona_api.tasks.handler import register_task_leg_handler

    registry = JobRegistry()
    register_task_leg_handler(
        registry,
        task_store=TaskStore(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
        runner_builder=_OneRunnerBuilder(runner),  # type: ignore[arg-type]
        continuation=TaskContinuation(
            task_store=TaskStore(app_engine),
            queue=JobQueue(app_engine),
            checkpoint_store=CheckpointStore(app_engine),
        ),
        rls_engine=app_engine,
    )
    return registry


class _NoRunnerBuilder:
    """A runner builder that must never be reached: the guard skips before any leg is built."""

    def build(self, task_id: str, persona_id: str, box: object, *, task: object = None) -> object:
        raise AssertionError(
            f"the guard should have skipped {task_id} ({persona_id}, {box}, {task})"
        )


# --- the gaps the owner's mutations found -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_task_whose_leg_is_running_right_now_is_left_alone(
    app_engine: Engine, su_engine: Engine
) -> None:
    """A leg in flight is the case where reviving does real damage: the same work would run
    twice, bill twice, and two legs would race the same checkpoint. `queued` is not the only
    live state, and a mutation dropping `running` from the set survived until this existed.
    """
    task = _task(app_engine, "t_running", "summarise the news", at=datetime.now(UTC))
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id=_UID,
        task_id=task.id,
        predecessor_seq=None,
        trigger=_fire(),
    )
    # Claim it and mark it running, exactly as the worker does before handing it to a handler.
    queue = JobQueue(su_engine)
    claimed = queue.claim(worker_id="w-inflight", lease_seconds=300, limit=20)
    job = next(j for j in claimed if j.payload.get("task_id") == task.id)
    for other in claimed:
        if other.id != job.id:
            queue.retry(
                job_id=other.id, worker_id="w-inflight", error="not mine", scheduled_at=_NOW
            )
    assert queue.mark_running(job_id=job.id, worker_id="w-inflight")
    _age(su_engine, task.id, by=STRANDED_GRACE + timedelta(minutes=1))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert [s for s, _ in _jobs(su_engine, task.id)] == ["running"]  # untouched, still one leg


@pytest.mark.asyncio
async def test_the_same_goal_on_a_different_persona_does_not_supersede(
    app_engine: Engine, su_engine: Engine
) -> None:
    """Supersession is about the user asking THIS persona for the same thing again. Two
    personas may hold the same goal honestly (a briefing each, from different sources), and
    reading one as the other's replacement silently drops work nobody cancelled."""
    with su_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y) "
                "ON CONFLICT DO NOTHING"
            ),
            {"i": "persona_other", "o": _UID, "y": "name: Iris"},
        )
    old = _task(
        app_engine, "t_mine", "brief hacker news", at=datetime.now(UTC) - timedelta(hours=2)
    )
    _age(su_engine, old.id, by=STRANDED_GRACE + timedelta(minutes=1))

    store = TaskStore(app_engine)
    store.create(
        Task(
            id="t_theirs",
            owner_id=_UID,
            persona_id="persona_other",  # a DIFFERENT persona, same words
            contract=Contract(goal="brief hacker news"),
            kind=TaskKind.AD_HOC,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    store.start(_UID, "t_theirs", now=datetime.now(UTC))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) >= 1
    assert [s for s, _ in _jobs(su_engine, old.id)] == ["queued"]  # revived, not written off


# --- the three fixes the owner required -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resume_that_raises_leaves_the_task_revivable_next_tick(
    app_engine: Engine, su_engine: Engine
) -> None:
    """The marker is written AFTER the enqueue.

    Written first, a resume that raised would leave a marker saying the task was saved when it
    was not, and it could never be revived at that head again: the sweep would strand the very
    task it exists to rescue. Written second, the worst case is a repeat attempt, and a repeat
    cannot double-run because the second resume computes the same idempotency key.
    """
    task = _task(app_engine, "t_raises", "brief hacker news", at=datetime.now(UTC))
    _age(su_engine, task.id, by=STRANDED_GRACE + timedelta(minutes=1))

    sweeper = _sweeper(app_engine, su_engine)
    real_resume = sweeper._continuation.resume  # noqa: SLF001 — the seam being made to fail
    calls = {"n": 0}

    def _fail_once(*args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            msg = "the database blinked"
            raise RuntimeError(msg)
        real_resume(*args, **kwargs)  # type: ignore[arg-type]

    sweeper._continuation.resume = _fail_once  # type: ignore[method-assign]  # noqa: SLF001
    assert await sweeper.run_once(now=datetime.now(UTC)) == 0  # it failed, and said so
    assert _jobs(su_engine, task.id) == []
    with su_engine.begin() as conn:
        marks = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = :a AND target = :t"),
            {"a": REVIVAL_AUDIT_ACTION, "t": task.id},
        ).scalar_one()
    assert marks == 0, "a failed revival must not claim it happened"

    # The next tick puts it back, which is the whole point.
    assert await sweeper.run_once(now=datetime.now(UTC)) == 1
    assert [s for s, _ in _jobs(su_engine, task.id)] == ["queued"]


@pytest.mark.asyncio
async def test_a_task_waiting_on_an_approval_is_never_revived(
    app_engine: Engine, su_engine: Engine
) -> None:
    """The shape that made head-blind matching act: a transient dead leg at this head, the
    user picked it up, and the task then parked on an APPROVAL at the same head. It waits on
    a decision, not on the world, and resuming would run a leg past the very refusal the reply
    route enforces."""
    from persona.approvals import ActionProposal
    from persona.tools import ActionCategory
    from persona_api.approvals.store import ApprovalStore

    task = _task(app_engine, "t_approval", "email the landlord", at=datetime.now(UTC))
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id=_UID,
        task_id=task.id,
        predecessor_seq=None,
        trigger=_fire(),
    )
    _dead_letter(su_engine, task.id, "429 rate limit")
    _park_on_the_dead_leg(app_engine, su_engine, task.id)
    ApprovalStore(app_engine).create_proposal(
        ActionProposal(
            proposal_id="prop_revival",
            owner_id=_UID,
            task_id=task.id,
            persona_id=_PERSONA,
            categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
            tool_name="send_email",
            arguments={"to": "landlord@example.com"},
            description="Email the landlord about the deposit",
            created_at=datetime.now(UTC),
        )
    )
    _age(su_engine, task.id, by=TRANSIENT_RETRY_AFTER + timedelta(minutes=1))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert [s for s, _ in _jobs(su_engine, task.id)] == ["dead"]  # nothing enqueued


@pytest.mark.asyncio
async def test_a_dead_leg_at_an_older_head_is_not_read_as_a_fresh_failure(
    app_engine: Engine, su_engine: Engine
) -> None:
    """A dead row is only current at the head it died on. Once the task has moved past that
    head, reading it again would resume work for a failure that has already been dealt with."""
    task = _task(app_engine, "t_old_head", "brief hacker news", at=datetime.now(UTC))
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id=_UID,
        task_id=task.id,
        predecessor_seq=None,
        trigger=_fire(),
    )
    _dead_letter(su_engine, task.id, "429 rate limit")
    _park_on_the_dead_leg(app_engine, su_engine, task.id)
    # The task moved on: a checkpoint landed, so the head is no longer where the leg died.
    _advance_head(su_engine, task.id, 0)
    _age(su_engine, task.id, by=TRANSIENT_RETRY_AFTER + timedelta(minutes=1))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 0
    assert [s for s, _ in _jobs(su_engine, task.id)] == ["dead"]


@pytest.mark.asyncio
async def test_the_revived_leg_is_never_told_the_user_replied(
    app_engine: Engine, su_engine: Engine
) -> None:
    """The sweep must not pose as the user.

    A `UserReply` trigger renders as "the user replied: ..." in the next leg's reconstruction,
    so the persona would be told someone said something they never said — and, since a reply
    is what clears open questions, it would also treat an unanswered question as settled.
    """
    from persona.tasks import AutoRetry, reconstruct_context

    task = _task(app_engine, "t_honest", "brief hacker news", at=datetime.now(UTC))
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id=_UID,
        task_id=task.id,
        predecessor_seq=None,
        trigger=_fire(),
    )
    _dead_letter(su_engine, task.id, "429 rate limit exceeded")
    _park_on_the_dead_leg(app_engine, su_engine, task.id)
    _age(su_engine, task.id, by=TRANSIENT_RETRY_AFTER + timedelta(minutes=1))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 1

    trigger = _queued_trigger(su_engine, task.id)
    assert trigger["kind"] == "auto_retry"
    # And what the next leg will actually READ says what happened, in the persona's own context.
    rendered = "\n".join(
        block.content
        for block in reconstruct_context(
            contract=TaskStore(app_engine).get(_UID, task.id).contract,
            trigger=AutoRetry(cause=trigger["cause"], retried_at=datetime.now(UTC)),
            checkpoint=None,
            recent_legs=(),
            retrieval=(),
        )
    )
    assert "the user replied" not in rendered
    assert "retried automatically after a temporary failure" in rendered
    assert "429 rate limit exceeded" in rendered  # it knows WHAT failed, so it can retry that


def _advance_head(su_engine: Engine, task_id: str, seq: int) -> None:
    """Move the task's head, the way an appended checkpoint does."""
    with su_engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET head_checkpoint_seq = :s WHERE id = :t"),
            {"s": seq, "t": task_id},
        )


def _queued_trigger(su_engine: Engine, task_id: str) -> dict[str, str]:
    """The trigger the sweep put on the queued leg."""
    with su_engine.begin() as conn:
        payload = conn.execute(
            text(
                "SELECT payload FROM jobs WHERE type = 'task_leg' AND state = 'queued' "
                "AND payload->>'task_id' = :t ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": task_id},
        ).scalar_one()
    return dict(payload["trigger"])


@pytest.mark.asyncio
async def test_a_stranded_leg_is_not_told_a_schedule_fired(
    app_engine: Engine, su_engine: Engine
) -> None:
    """Spec W1 (D-W1-38): the stranded half carried the same untruth AutoRetry removed from the
    transient half. Nothing failed and no schedule fired; the leg was simply never run, so that
    is what the next one is told."""
    from persona.tasks import Revived, reconstruct_context

    task = _task(app_engine, "t_honest_stranded", "brief hacker news", at=datetime.now(UTC))
    _age(su_engine, task.id, by=STRANDED_GRACE + timedelta(minutes=1))

    assert await _sweeper(app_engine, su_engine).run_once(now=datetime.now(UTC)) == 1

    trigger = _queued_trigger(su_engine, task.id)
    assert trigger["kind"] == "revived"
    rendered = "\n".join(
        block.content
        for block in reconstruct_context(
            contract=TaskStore(app_engine).get(_UID, task.id).contract,
            trigger=Revived(reason=trigger["reason"], revived_at=datetime.now(UTC)),
            checkpoint=None,
            recent_legs=(),
            retrieval=(),
        )
    )
    assert "scheduled fire" not in rendered
    assert "the user replied" not in rendered
    assert "put back to work" in rendered
