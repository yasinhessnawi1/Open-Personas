"""A0 integration sweep — real worker loops, real Postgres (Spec A0, T11).

The cohesive end-to-end pass over the acceptance criteria, exercising the ACTUAL
``Worker.run()`` loop (not just ``executor.execute``):

- **criterion 1** (enqueue/claim/complete under concurrency, no double-claim) +
  **2** (idempotent avatar) + **6** (fairness) — two real worker loops drain a
  mixed multi-user workload; every job succeeds exactly once.
- **criterion 3** (crash-resume) — a job a dead worker left ``running`` with an
  expired lease is reclaimed by a real worker's maintenance sweep and completed.

Criteria 4 (drain), 5 (retry/dead-letter), 7-via-construction, and the per-
mechanism proofs live in the focused T5/T6/T9 suites; this sweep ties the loop
together. The live chat path is untouched by construction (the queue is additive;
``ConversationLoop`` has no job dependency) — confirmed by the green api suite.
"""

# ruff: noqa: ARG001, ARG002, SLF001 — fixtures + protocol args + private internals.
from __future__ import annotations

import asyncio
import os

import pytest
from persona.jobs import SHORT_LEASE, JobPayload, JobRegistry, JobTypeSpec
from persona_api.jobs import JobQueue, Worker
from persona_api.jobs.handlers.avatar import (
    AVATAR_JOB_TYPE,
    AvatarGenerationHandler,
    AvatarGenerationPayload,
    AvatarResult,
    avatar_idempotency_key,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping A0 sweep")
    return create_engine(app_url.replace("+asyncpg", "+psycopg"))


class _NoopPayload(JobPayload):
    pass


class _NoopHandler:
    async def handle(self, payload: _NoopPayload, context: object) -> None:
        return


class _FakeAvatarGen:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    async def generate(
        self, *, persona_id: str, owner_id: str, yaml_str: str, billing_key: str
    ) -> AvatarResult:
        self.calls[persona_id] = self.calls.get(persona_id, 0) + 1
        return AvatarResult(avatar_url=f"avatars/{persona_id}.png", cost_micros=1, provider="fake")


def _registry(gen: _FakeAvatarGen) -> JobRegistry:
    return JobRegistry(
        [
            JobTypeSpec(
                type=AVATAR_JOB_TYPE,
                payload_model=AvatarGenerationPayload,
                handler=AvatarGenerationHandler(generator=gen),  # type: ignore[arg-type]
                idempotency_key=lambda p: avatar_idempotency_key(p.persona_id),
                lease=SHORT_LEASE,
            ),
            JobTypeSpec(
                type="noop",
                payload_model=_NoopPayload,
                handler=_NoopHandler(),  # type: ignore[arg-type]
                idempotency_key=lambda _p: "noop",
                lease=SHORT_LEASE,
            ),
        ]
    )


def _state_counts(engine: Engine) -> dict[str, int]:
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT state, count(*) FROM jobs GROUP BY state")).all()
    return {r[0]: r[1] for r in rows}


def test_two_real_workers_drain_mixed_workload(migrated_engine: Engine, app_engine: Engine) -> None:
    # Seed two users + 10 personas; enqueue 10 avatar jobs + 10 noop jobs.
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('ua','a@x'),('ub','b@x')"))
        for i in range(10):
            owner = "ua" if i % 2 == 0 else "ub"
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
                {"p": f"p{i}", "o": owner},
            )
    queue = JobQueue(migrated_engine)
    for i in range(10):
        owner = "ua" if i % 2 == 0 else "ub"
        queue.enqueue(
            type=AVATAR_JOB_TYPE,
            owner_id=owner,
            payload={"persona_id": f"p{i}"},
            idempotency_key=avatar_idempotency_key(f"p{i}"),
        )
        queue.enqueue(type="noop", owner_id=owner, payload={}, idempotency_key=f"noop:{i}")

    gen = _FakeAvatarGen()
    registry = _registry(gen)

    def _mk() -> Worker:
        return Worker(
            dispatch_engine=migrated_engine,
            rls_engine=app_engine,
            registry=registry,
            concurrency=4,
            poll_interval_seconds=0.02,
            poll_jitter_seconds=0.0,
            max_jobs_per_user=3,
            maintenance_interval_seconds=0.3,
        )

    async def drive() -> None:
        w1, w2 = _mk(), _mk()
        t1 = asyncio.create_task(w1.run())
        t2 = asyncio.create_task(w2.run())
        for _ in range(300):
            if _state_counts(migrated_engine).get("succeeded", 0) == 20:
                break
            await asyncio.sleep(0.05)
        w1.request_drain()
        w2.request_drain()
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=15)

    asyncio.run(drive())

    counts = _state_counts(migrated_engine)
    assert counts.get("succeeded", 0) == 20, (
        f"every job must complete exactly once; states={counts}"
    )
    # Each persona's avatar generated exactly once (idempotent; no double-claim).
    assert all(v == 1 for v in gen.calls.values()), f"double-generation: {gen.calls}"
    assert len(gen.calls) == 10
    with migrated_engine.begin() as conn:
        avatars = conn.execute(
            text("SELECT count(*) FROM personas WHERE avatar_url IS NOT NULL")
        ).scalar_one()
    assert avatars == 10


def test_crash_resume_by_real_worker_maintenance(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    # A dead worker left a job 'running' with an EXPIRED lease (claimed, then the
    # process vanished before doing the work). A real worker's maintenance sweep
    # must reclaim it and the loop must complete it.
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('ua','a@x')"))
        conn.execute(
            text(
                "INSERT INTO jobs (id, type, owner_id, idempotency_key, state, locked_by, "
                "lease_expires_at, attempt) VALUES "
                "('j1','noop','ua','noop','running','deadWorker', now()-interval '1 minute', 1)"
            )
        )
    worker = Worker(
        dispatch_engine=migrated_engine,
        rls_engine=app_engine,
        registry=_registry(_FakeAvatarGen()),
        worker_id="resumer",
        poll_interval_seconds=0.02,
        poll_jitter_seconds=0.0,
        maintenance_interval_seconds=0.2,
    )

    async def drive() -> None:
        run_task = asyncio.create_task(worker.run())
        for _ in range(200):
            with migrated_engine.begin() as conn:
                state = conn.execute(text("SELECT state FROM jobs WHERE id='j1'")).scalar_one()
            if state == "succeeded":
                break
            await asyncio.sleep(0.05)
        worker.request_drain()
        await asyncio.wait_for(run_task, timeout=15)

    asyncio.run(drive())

    with migrated_engine.begin() as conn:
        row = conn.execute(text("SELECT state, locked_by FROM jobs WHERE id='j1'")).one()
    assert row.state == "succeeded", "the crashed-worker's job must be reclaimed + completed"
