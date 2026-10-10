"""Retry / backoff / dead-letter tests (Spec A0, T6) — criterion 5.

Against real Postgres. Proves: a transient handler failure schedules a retry with
capped-exponential backoff (T1 ``RetryPolicy.backoff_for``); exhaustion
dead-letters durably as ``dead`` with the cause and is queryable; a
``PermanentJobError`` terminates as ``failed`` (distinct from ``dead``); and the
caps hold (no retry storm — a dead/failed job is never re-claimed; a crash-loop
re-claim past the cap dead-letters without running).
"""

# ruff: noqa: ARG001, ARG002, SLF001 — fixtures + protocol args + private internals.
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.errors import JobDeferredError, PermanentJobError
from persona.jobs import JobPayload, JobRegistry, JobState, JobTypeSpec, RetryPolicy
from persona_api.jobs import JobQueue
from persona_api.jobs.executor import JobExecutor
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

if TYPE_CHECKING:
    from persona.jobs import JobContext

pytestmark = pytest.mark.integration


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping retry test")
    return create_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('user_a','a@example.com')"))
    return migrated_engine


class _Payload(JobPayload):
    marker: str = "m"


class _Boom:
    """Handler that always raises — transient unless ``permanent`` is set."""

    def __init__(self, *, permanent: bool = False) -> None:
        self.permanent = permanent
        self.calls = 0

    async def handle(self, payload: _Payload, context: JobContext) -> None:
        self.calls += 1
        if self.permanent:
            raise PermanentJobError("permanently broken", context={"marker": payload.marker})
        msg = "transient failure"
        raise RuntimeError(msg)


def _executor(
    engine: Engine, app_engine: Engine, handler: object, retry: RetryPolicy
) -> JobExecutor:
    registry = JobRegistry(
        [
            JobTypeSpec(
                type="boom",
                payload_model=_Payload,
                handler=handler,  # type: ignore[arg-type]
                idempotency_key=lambda p: f"boom:{p.marker}",
                retry=retry,
            )
        ]
    )
    return JobExecutor(
        queue=JobQueue(engine), registry=registry, rls_engine=app_engine, worker_id="w1"
    )


def _row(engine: Engine, job_id: str) -> tuple[str, str | None, datetime]:
    with engine.begin() as conn:
        r = conn.execute(
            text("SELECT state, last_error, scheduled_at FROM jobs WHERE id = :i"), {"i": job_id}
        ).one()
    return r.state, r.last_error, r.scheduled_at


async def _claim_and_execute(executor: JobExecutor, queue: JobQueue) -> JobState:
    claimed = queue.claim(worker_id="w1", lease_seconds=30, limit=1)
    assert claimed, "expected a claimable job"
    return await executor.execute(claimed[0])


def test_transient_failure_schedules_retry_with_backoff(seeded: Engine, app_engine: Engine) -> None:
    policy = RetryPolicy(max_attempts=3, base_backoff_seconds=10.0, max_backoff_seconds=100.0)
    handler = _Boom()
    queue = JobQueue(seeded)
    executor = _executor(seeded, app_engine, handler, policy)
    rec = queue.enqueue(
        type="boom", owner_id="user_a", payload={"marker": "m"}, idempotency_key="boom:m"
    )
    assert rec is not None

    import asyncio

    t0 = datetime.now(UTC)
    outcome = asyncio.run(_claim_and_execute(executor, queue))
    t1 = datetime.now(UTC)

    assert outcome is JobState.QUEUED
    state, last_error, scheduled_at = _row(seeded, rec.id)
    assert state == "queued"
    assert last_error is not None
    assert "transient" in last_error
    # backoff_for(attempt=1) = 10s; equal-jitter → [5s, 10s] ahead of execution time.
    assert t0 + timedelta(seconds=4) <= scheduled_at <= t1 + timedelta(seconds=11)


def test_permanent_error_terminates_as_failed_not_dead(seeded: Engine, app_engine: Engine) -> None:
    handler = _Boom(permanent=True)
    queue = JobQueue(seeded)
    executor = _executor(seeded, app_engine, handler, RetryPolicy(max_attempts=5))
    rec = queue.enqueue(
        type="boom", owner_id="user_a", payload={"marker": "m"}, idempotency_key="boom:m"
    )
    assert rec is not None

    import asyncio

    outcome = asyncio.run(_claim_and_execute(executor, queue))

    assert outcome is JobState.FAILED  # FAILED (permanent), NOT dead (exhausted)
    state, last_error, _ = _row(seeded, rec.id)
    assert state == "failed"
    assert last_error is not None
    assert "permanently broken" in last_error
    assert handler.calls == 1, "a permanent failure must not be retried"


def test_exhaustion_dead_letters_with_cause_and_is_queryable(
    seeded: Engine, app_engine: Engine
) -> None:
    # max_attempts=2, near-zero backoff so the retry is immediately re-claimable.
    policy = RetryPolicy(max_attempts=2, base_backoff_seconds=0.01, max_backoff_seconds=0.01)
    handler = _Boom()
    queue = JobQueue(seeded)
    executor = _executor(seeded, app_engine, handler, policy)
    rec = queue.enqueue(
        type="boom", owner_id="user_a", payload={"marker": "m"}, idempotency_key="boom:m"
    )
    assert rec is not None

    import asyncio

    async def scenario() -> tuple[JobState, JobState]:
        first = await _claim_and_execute(executor, queue)  # attempt 1 → retry
        await asyncio.sleep(0.05)  # let the tiny backoff elapse
        second = await _claim_and_execute(executor, queue)  # attempt 2 → dead
        return first, second

    first, second = asyncio.run(scenario())
    assert first is JobState.QUEUED
    assert second is JobState.DEAD
    assert handler.calls == 2  # exactly max_attempts executions — no storm
    state, last_error, _ = _row(seeded, rec.id)
    assert state == "dead"
    assert last_error is not None
    assert "transient" in last_error
    # Queryable as a dead-letter (the A3/A6 seam).
    dead = queue.dead_letters()
    assert [d.id for d in dead] == [rec.id]


def test_dead_job_is_not_reclaimable_no_storm(seeded: Engine, app_engine: Engine) -> None:
    policy = RetryPolicy(max_attempts=1)  # one shot, then dead
    queue = JobQueue(seeded)
    executor = _executor(seeded, app_engine, _Boom(), policy)
    queue.enqueue(type="boom", owner_id="user_a", payload={"marker": "m"}, idempotency_key="boom:m")

    import asyncio

    outcome = asyncio.run(_claim_and_execute(executor, queue))
    assert outcome is JobState.DEAD
    # A terminal dead job is NEVER claimed again (the cap holds — no retry storm).
    assert queue.claim(worker_id="w1", lease_seconds=30, limit=10) == []


def test_crash_loop_cap_dead_letters_without_running(seeded: Engine, app_engine: Engine) -> None:
    # Simulate a job re-claimed past its cap by a series of crashes (lease-expiry
    # reclaim never runs the dead-letter path): force attempt high, then claim.
    policy = RetryPolicy(max_attempts=2)
    handler = _Boom()
    queue = JobQueue(seeded)
    executor = _executor(seeded, app_engine, handler, policy)
    rec = queue.enqueue(
        type="boom", owner_id="user_a", payload={"marker": "m"}, idempotency_key="boom:m"
    )
    assert rec is not None
    with seeded.begin() as conn:
        conn.execute(text("UPDATE jobs SET attempt = 2 WHERE id = :i"), {"i": rec.id})  # at the cap

    import asyncio

    # claim → attempt becomes 3 (> max_attempts=2) → dead-letter WITHOUT running.
    outcome = asyncio.run(_claim_and_execute(executor, queue))
    assert outcome is JobState.DEAD
    assert handler.calls == 0, "a job past its attempt cap must not run the handler again"
    state, last_error, _ = _row(seeded, rec.id)
    assert state == "dead"
    assert last_error == "max attempts exceeded"


class _Paused:
    """Handler that defers its job (D-FV0-31: the document indexer's lane pause)."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, payload: _Payload, context: JobContext) -> None:
        self.calls += 1
        msg = "paused"
        raise JobDeferredError(msg, delay_seconds=120.0)


def test_a_deferred_job_goes_back_due_later_at_the_same_attempt(
    seeded: Engine, app_engine: Engine
) -> None:
    import asyncio

    handler = _Paused()
    queue = JobQueue(seeded)
    executor = _executor(seeded, app_engine, handler, RetryPolicy(max_attempts=2))
    rec = queue.enqueue(
        type="boom", owner_id="user_a", payload={"marker": "m"}, idempotency_key="boom:m"
    )
    assert rec is not None
    for _ in range(4):  # more deferrals than attempts: never dead-lettered
        with seeded.begin() as conn:
            conn.execute(text("UPDATE jobs SET scheduled_at = now() WHERE id = :i"), {"i": rec.id})
        t0 = datetime.now(UTC)
        outcome = asyncio.run(_claim_and_execute(executor, queue))
        assert outcome is JobState.QUEUED
        state, last_error, scheduled_at = _row(seeded, rec.id)
        assert state == "queued"
        assert last_error is not None
        assert "paused" in last_error
        assert scheduled_at >= t0 + timedelta(seconds=110)
        with seeded.begin() as conn:
            attempt = conn.execute(
                text("SELECT attempt FROM jobs WHERE id = :i"), {"i": rec.id}
            ).scalar_one()
        assert attempt == 0
    assert handler.calls == 4
