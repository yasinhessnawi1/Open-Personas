"""The dead-leg sweep — a retry-exhausted task parks on the user + is voiced (Spec A3, T13).

Runs against real Postgres. Proves the GAP #1b wiring: a dead ``task_leg`` job (A0 exhausted its
retries → ``jobs.state='dead'``) drives the task ``active → waiting(on_user)`` with an honest
StuckReport, and that report is voiced once through the C0 failure notifier (asserted via a spy).
Idempotent: a second sweep of the same dead job re-voices nothing (the task is no longer active).
The memory-backend gate: with no notifier the task still parks (persist-only floor).
"""

# ruff: noqa: ARG001, ARG002 — ``migrated_engine`` fixture param + the spy's Protocol-shaped args.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.tasks import Contract, ScheduledFire, Task, TaskState, WaitKind
from persona_api.approvals.failure import FailureAccount, FailureKind
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.dead_leg_sweep import DeadLegSweeper
from persona_api.tasks.handler import enqueue_task_leg
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    # Build the RLS engine with the checkout listener (the production worker's engine) so the
    # voicing path's contextvar-driven owner scope is honoured, exactly as in the worker loop.
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    token = current_user_id.set(None)  # start each test with no ambient scope (the sweep sets it)
    try:
        yield engine
    finally:
        current_user_id.reset(token)
        engine.dispose()


class _FakeLeader:
    """Stand-in for ``SchedulerLeader`` (its advisory-lock election is separately tested)."""

    def __init__(self, *, is_leader: bool = True) -> None:
        self._is_leader = is_leader

    def try_become_leader(self) -> bool:
        return self._is_leader


class _SpyFailureNotifier:
    """Records each voiced account, so the wiring test asserts the C0 failure path fired."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, FailureKind, str, str]] = []

    async def notify(
        self,
        account: FailureAccount,
        *,
        persona: object,
        owner_id: str,
        conversation_id: str,
    ) -> None:
        self.calls.append((account.task_id, account.kind, owner_id, conversation_id))


def _seed_active_task_with_conversation(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('user_a','a@example.com')"))
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES ('persona_a','user_a','name: x')"
            )
        )
        # tasks.conversation_id carries an FK to conversations — seed the row the task links to.
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id) "
                "VALUES ('conv_1','user_a','persona_a')"
            )
        )
    tasks = TaskStore(engine)
    tasks.create(
        Task(
            id="t1",
            owner_id="user_a",
            persona_id="persona_a",
            contract=Contract(goal="find the cheapest fare"),
            conversation_id="conv_1",
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start("user_a", "t1", now=_NOW)


def _enqueue_dead_task_leg(app_engine: Engine, migrated_engine: Engine) -> None:
    """Enqueue a real ``task_leg`` job (RLS-scoped) then force it to A0's dead-letter state."""
    enqueue_task_leg(
        JobQueue(app_engine),
        owner_id="user_a",
        task_id="t1",
        predecessor_seq=None,
        trigger=ScheduledFire(schedule_id="self:t1", fire_time=_NOW),
    )
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE jobs SET state = 'dead', last_error = 'provider 500 after 5 tries' "
                "WHERE type = 'task_leg' AND owner_id = 'user_a'"
            )
        )


def _sweeper(
    migrated_engine: Engine, app_engine: Engine, notifier: object | None, *, is_leader: bool = True
) -> DeadLegSweeper:
    continuation = TaskContinuation(
        task_store=TaskStore(app_engine),
        queue=JobQueue(app_engine),
        checkpoint_store=CheckpointStore(app_engine),
    )
    return DeadLegSweeper(
        continuation=continuation,
        dead_letter_queue=JobQueue(migrated_engine),  # cross-tenant privileged read
        leader=_FakeLeader(is_leader=is_leader),  # type: ignore[arg-type]
        rls_engine=app_engine,
        task_store=TaskStore(app_engine),
        notifier=notifier,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_dead_leg_parks_task_and_voices_once(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_active_task_with_conversation(migrated_engine)
    _enqueue_dead_task_leg(app_engine, migrated_engine)
    spy = _SpyFailureNotifier()
    sweeper = _sweeper(migrated_engine, app_engine, spy)

    reacted = await sweeper.run_once(now=_NOW)

    # The task transitioned active → waiting(on_user) (no longer a silent orphan).
    task = TaskStore(app_engine).get("user_a", "t1")
    assert task.state == TaskState.WAITING
    assert task.wait_kind == WaitKind.ON_USER
    assert reacted == 1
    # The StuckReport was voiced once on the task's conversation, as a leg-dead-letter account.
    assert spy.calls == [("t1", FailureKind.LEG_DEAD_LETTER, "user_a", "conv_1")]


@pytest.mark.asyncio
async def test_dead_leg_sweep_is_idempotent(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_active_task_with_conversation(migrated_engine)
    _enqueue_dead_task_leg(app_engine, migrated_engine)
    spy = _SpyFailureNotifier()
    sweeper = _sweeper(migrated_engine, app_engine, spy)

    await sweeper.run_once(now=_NOW)
    second = await sweeper.run_once(now=_NOW)  # the same dead job re-read next cadence

    # The task is already parked → react_to_dead_leg is a no-op → no re-voice.
    assert second == 0
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_dead_leg_non_leader_is_a_no_op(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_active_task_with_conversation(migrated_engine)
    _enqueue_dead_task_leg(app_engine, migrated_engine)
    spy = _SpyFailureNotifier()
    sweeper = _sweeper(migrated_engine, app_engine, spy, is_leader=False)

    reacted = await sweeper.run_once(now=_NOW)

    assert reacted == 0
    assert spy.calls == []
    assert TaskStore(app_engine).get("user_a", "t1").state == TaskState.ACTIVE  # untouched


@pytest.mark.asyncio
async def test_dead_leg_without_backend_still_parks(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    # The memory-backend gate: notifier=None still parks the task waiting(on_user); the voiced
    # C0 account degrades to the persist-only floor (the Tasks surface still shows the stuck task).
    _seed_active_task_with_conversation(migrated_engine)
    _enqueue_dead_task_leg(app_engine, migrated_engine)
    sweeper = _sweeper(migrated_engine, app_engine, None)

    reacted = await sweeper.run_once(now=_NOW)

    assert reacted == 1
    task = TaskStore(app_engine).get("user_a", "t1")
    assert task.state == TaskState.WAITING
    assert task.wait_kind == WaitKind.ON_USER
