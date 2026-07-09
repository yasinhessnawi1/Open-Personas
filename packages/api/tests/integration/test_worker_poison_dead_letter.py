"""Pre-dispatch poison jobs dead-letter decisively (R9-013) — no unretrieved-exception loop.

Against real Postgres, through the REAL :meth:`Worker.run_once` claim→execute
path. Pre-fix, a claimed job whose type had no registered handler raised
``UnknownJobTypeError`` OUT of the asyncio task ("Task exception was never
retrieved"), was never failed, lease-lapsed, reclaimed, and re-crashed every
cycle — forever. Proves:

- an unknown-type job lands ``state='dead'`` with the honest cause on the FIRST
  run, is NOT re-claimed on a second ``run_once``, and no unretrieved-exception
  report ever fires (asserted via a custom event-loop exception handler);
- the ``create_task`` dispatch path (the continuous ``Worker.run`` shape)
  settles the task handled — ``task.exception()`` is ``None``;
- a durable payload that fails its registered model's validation (the same
  deterministic pre-dispatch class) dead-letters too;
- genuinely-transient handler failures still retry (unchanged — covered by
  ``test_worker_retry.py``; re-asserted here only via the dead job's terminality).
"""

# ruff: noqa: ARG001, ARG002, SLF001 — fixtures + protocol args + private internals.
from __future__ import annotations

import asyncio
import gc
import os
from datetime import datetime  # noqa: TC003 — runtime use in helper annotation
from typing import TYPE_CHECKING, Any

import pytest
from persona.jobs import JobPayload, JobRegistry, JobState, JobTypeSpec, RetryPolicy
from persona_api.jobs import JobQueue
from persona_api.jobs.executor import JobExecutor
from persona_api.jobs.worker import Worker
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

if TYPE_CHECKING:
    from persona.jobs import JobContext

pytestmark = pytest.mark.integration


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping poison dead-letter test")
    return create_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('user_a','a@example.com')"))
    return migrated_engine


class _StrictPayload(JobPayload):
    required_marker: str  # no default — a payload without it fails validation


class _Recorder:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, payload: _StrictPayload, context: JobContext) -> None:
        self.calls += 1


def _row(engine: Engine, job_id: str) -> tuple[str, str | None, datetime]:
    with engine.begin() as conn:
        r = conn.execute(
            text("SELECT state, last_error, scheduled_at FROM jobs WHERE id = :i"), {"i": job_id}
        ).one()
    return r.state, r.last_error, r.scheduled_at


def test_unknown_type_dead_letters_on_first_real_worker_run(
    seeded: Engine, app_engine: Engine
) -> None:
    """The R9-013 trigger chain: real claim → real execute → decisive dead-letter."""
    queue = JobQueue(seeded)
    # The owner's production shape: an ``avatar_generation`` job enqueued by the
    # route, claimed by a worker whose registry never registered the handler.
    rec = queue.enqueue(
        type="avatar_generation",
        owner_id="user_a",
        payload={"persona_id": "p1"},
        idempotency_key="avatar:p1:create",
    )
    assert rec is not None
    worker = Worker(
        dispatch_engine=seeded,
        rls_engine=app_engine,
        registry=JobRegistry(),  # NO handler for the type — the half-shipped cutover
        worker_id="w1",
    )

    async def scenario() -> tuple[int, int, list[dict[str, Any]]]:
        captured: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, ctx: captured.append(dict(ctx)))
        first = await worker.run_once(batch=5)
        second = await worker.run_once(batch=5)  # a dead job must NOT be re-claimed
        # Flush any doomed task so an unretrieved exception would report NOW.
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        return first, second, captured

    first, second, captured = asyncio.run(scenario())

    assert first == 1  # claimed + executed (the dead-letter path ran)
    assert second == 0  # terminal — no reclaim, no poison loop
    state, last_error, _ = _row(seeded, rec.id)
    assert state == "dead"
    assert last_error is not None
    assert "unknown job type" in last_error
    assert captured == [], f"unretrieved-exception report fired: {captured}"
    # Queryable as a dead-letter (the A3/A6 observability seam).
    assert [d.id for d in queue.dead_letters()] == [rec.id]


def test_create_task_dispatch_settles_handled(seeded: Engine, app_engine: Engine) -> None:
    """The continuous-loop dispatch shape: execute() as a task completes HANDLED."""
    queue = JobQueue(seeded)
    rec = queue.enqueue(
        type="ghost_type",
        owner_id="user_a",
        payload={},
        idempotency_key="ghost:1",
    )
    assert rec is not None
    executor = JobExecutor(
        queue=JobQueue(seeded), registry=JobRegistry(), rls_engine=app_engine, worker_id="w1"
    )

    async def scenario() -> tuple[JobState, BaseException | None]:
        claimed = queue.claim(worker_id="w1", lease_seconds=30, limit=1)
        assert claimed, "expected a claimable job"
        task: asyncio.Task[JobState] = asyncio.create_task(executor.execute(claimed[0]))
        outcome = await asyncio.wait_for(task, timeout=10)
        return outcome, task.exception()

    outcome, exc = asyncio.run(scenario())
    assert outcome is JobState.DEAD
    assert exc is None, "nothing may escape the job task (unretrieved-exception poison)"
    state, last_error, _ = _row(seeded, rec.id)
    assert state == "dead"
    assert last_error is not None
    assert "unknown job type" in last_error


def test_malformed_durable_payload_dead_letters(seeded: Engine, app_engine: Engine) -> None:
    """The same poison class: a payload that can never validate — dead; handler never runs."""
    handler = _Recorder()
    registry = JobRegistry(
        [
            JobTypeSpec(
                type="strict",
                payload_model=_StrictPayload,
                handler=handler,
                idempotency_key=lambda p: f"strict:{p.required_marker}",
                retry=RetryPolicy(max_attempts=3),
            )
        ]
    )
    queue = JobQueue(seeded)
    # Enqueue bypasses the registry (the route-side producer's shape), so a
    # schema-drifted payload can land durably; validation fires at claim time.
    rec = queue.enqueue(
        type="strict", owner_id="user_a", payload={"wrong": "shape"}, idempotency_key="strict:x"
    )
    assert rec is not None
    worker = Worker(
        dispatch_engine=seeded, rls_engine=app_engine, registry=registry, worker_id="w1"
    )

    async def scenario() -> tuple[int, int]:
        first = await worker.run_once(batch=5)
        second = await worker.run_once(batch=5)
        return first, second

    first, second = asyncio.run(scenario())
    assert (first, second) == (1, 0)
    assert handler.calls == 0, "an unparseable payload must never reach the handler"
    state, last_error, _ = _row(seeded, rec.id)
    assert state == "dead"
    assert last_error is not None
