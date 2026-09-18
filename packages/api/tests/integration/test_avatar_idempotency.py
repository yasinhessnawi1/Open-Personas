"""At-least-once idempotency proof for the avatar handler (Spec A0, T9) — criterion 2.

THE HARD GATE. Avatar generation is non-deterministic, so the contract is "exactly
ONE valid avatar_url after re-delivery, no corruption, no orphan" — not byte
identity. Proven against the REAL :class:`AvatarGenerationHandler` through BOTH
T6 re-delivery paths:

- **handler-raise → retry** (died after persisting bytes, before the set): the
  job retries; the regeneration OVERWRITES the deterministic per-persona path
  (no orphan) and the compare-and-set lands exactly one url.
- **lease-expiry → reclaim** (worker died before ``complete``, side effect already
  done): the reclaim re-runs the handler, which SKIPS (avatar_url already set) —
  a true no-op, no regeneration.

A handler that can't prove this doesn't ship.
"""

# ruff: noqa: ARG001, ARG002, SLF001 — fixtures + protocol args + private internals.
from __future__ import annotations

import asyncio
import os

import pytest
from persona.jobs import JobRegistry, JobState, JobTypeSpec, RetryPolicy
from persona_api.jobs import JobQueue, WorkerJobContext
from persona_api.jobs.executor import JobExecutor
from persona_api.jobs.handlers.avatar import (
    AVATAR_JOB_TYPE,
    AvatarGenerationHandler,
    AvatarGenerationPayload,
    AvatarResult,
    avatar_idempotency_key,
    enqueue_avatar_generation,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping avatar idempotency test")
    return create_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def persona(migrated_engine: Engine) -> Engine:
    with migrated_engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('user_a','a@example.com')"))
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p1','user_a','name: A')")
        )
    return migrated_engine


class _FakeGenerator:
    """Deterministic per-persona path (overwrite-safe → no orphan). Counts calls.

    ``fail_after_persist`` makes the FIRST call persist bytes then raise — modelling
    a worker that died after generating but before the avatar_url was set.
    """

    def __init__(self, *, fail_after_persist: bool = False) -> None:
        self.calls = 0
        self.files: dict[str, str] = {}  # persona_id → url; assignment overwrites.
        self._fail_after_persist = fail_after_persist

    async def generate(
        self, *, persona_id: str, owner_id: str, yaml_str: str, billing_key: str
    ) -> AvatarResult | None:
        self.calls += 1
        url = f"avatars/{persona_id}.png"  # deterministic → re-gen overwrites, no orphan
        self.files[persona_id] = url
        if self._fail_after_persist and self.calls == 1:
            msg = "died after persist, before set"
            raise RuntimeError(msg)
        return AvatarResult(avatar_url=url, cost_micros=1000, provider="fake")


def _registry(generator: _FakeGenerator) -> JobRegistry:
    return JobRegistry(
        [
            JobTypeSpec(
                type=AVATAR_JOB_TYPE,
                payload_model=AvatarGenerationPayload,
                handler=AvatarGenerationHandler(generator=generator),  # type: ignore[arg-type]
                idempotency_key=lambda p: avatar_idempotency_key(p.persona_id),
                retry=RetryPolicy(
                    max_attempts=3, base_backoff_seconds=0.01, max_backoff_seconds=0.01
                ),
            )
        ]
    )


def _avatar_url(engine: Engine) -> str | None:
    with engine.begin() as conn:
        return conn.execute(text("SELECT avatar_url FROM personas WHERE id='p1'")).scalar_one()


def _enqueue(engine: Engine) -> str:
    rec = JobQueue(engine).enqueue(
        type=AVATAR_JOB_TYPE,
        owner_id="user_a",
        payload={"persona_id": "p1"},
        idempotency_key=avatar_idempotency_key("p1"),
    )
    assert rec is not None
    return rec.id


# --- enqueue dedup --------------------------------------------------------------


def test_duplicate_enqueue_same_persona_is_a_noop(persona: Engine) -> None:
    queue = JobQueue(persona)
    enqueue_avatar_generation(queue, persona_id="p1", owner_id="user_a")
    enqueue_avatar_generation(queue, persona_id="p1", owner_id="user_a")  # same key
    with persona.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM jobs")).scalar_one()
    assert count == 1


# --- skip-if-already-set --------------------------------------------------------


def test_skip_if_already_set_does_not_regenerate(persona: Engine, app_engine: Engine) -> None:
    with persona.begin() as conn:
        conn.execute(text("UPDATE personas SET avatar_url='avatars/preexisting.png' WHERE id='p1'"))
    gen = _FakeGenerator()
    executor = JobExecutor(
        queue=JobQueue(persona), registry=_registry(gen), rls_engine=app_engine, worker_id="w1"
    )
    _enqueue(persona)
    out = asyncio.run(
        executor.execute(JobQueue(persona).claim(worker_id="w1", lease_seconds=30)[0])
    )
    assert out is JobState.SUCCEEDED
    assert gen.calls == 0, "skip-if-set must NOT regenerate"
    assert _avatar_url(persona) == "avatars/preexisting.png"


# --- THE GATE: re-delivery paths ------------------------------------------------


def test_lease_expiry_reclaim_after_side_effect_is_noop(
    persona: Engine, app_engine: Engine
) -> None:
    # Worker A: claim → run handler (SETS avatar) → DIES before complete().
    gen = _FakeGenerator()
    queue = JobQueue(persona)
    registry = _registry(gen)
    _enqueue(persona)
    rec = queue.claim(worker_id="wA", lease_seconds=30)[0]
    assert queue.mark_running(job_id=rec.id, worker_id="wA")
    ctx = WorkerJobContext(
        owner_id="user_a", rls_engine=app_engine, job_id=rec.id, job_type=AVATAR_JOB_TYPE
    )
    asyncio.run(
        AvatarGenerationHandler(generator=gen).handle(  # type: ignore[arg-type]
            AvatarGenerationPayload(persona_id="p1"), ctx
        )
    )
    assert gen.calls == 1
    url_after_first = _avatar_url(persona)
    assert url_after_first == "avatars/p1.png"  # side effect landed; worker then "died"

    # Lease expires → reclaim → worker B re-runs through the executor.
    with persona.begin() as conn:
        conn.execute(
            text("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id=:i"),
            {"i": rec.id},
        )
    assert queue.reclaim_expired() == 1
    executor = JobExecutor(queue=queue, registry=registry, rls_engine=app_engine, worker_id="wB")
    out = asyncio.run(executor.execute(queue.claim(worker_id="wB", lease_seconds=30)[0]))

    assert out is JobState.SUCCEEDED
    assert gen.calls == 1, "re-delivery after a completed side effect must NOT regenerate"
    assert _avatar_url(persona) == url_after_first  # exactly one, unchanged
    assert len(gen.files) == 1  # no orphan


def test_retry_path_yields_one_avatar_and_no_orphan(persona: Engine, app_engine: Engine) -> None:
    # Handler raises AFTER persisting bytes (died before set) → T6 retry → re-run.
    gen = _FakeGenerator(fail_after_persist=True)
    queue = JobQueue(persona)
    executor = JobExecutor(
        queue=queue, registry=_registry(gen), rls_engine=app_engine, worker_id="w1"
    )
    _enqueue(persona)

    async def scenario() -> tuple[JobState, JobState]:
        first = await executor.execute(queue.claim(worker_id="w1", lease_seconds=30)[0])
        await asyncio.sleep(0.05)  # tiny backoff elapses
        claimed = queue.claim(worker_id="w1", lease_seconds=30)
        assert claimed, "the retried job must be re-claimable"
        second = await executor.execute(claimed[0])
        return first, second

    first, second = asyncio.run(scenario())
    assert first is JobState.QUEUED  # raised → retry scheduled
    assert second is JobState.SUCCEEDED  # re-run set the avatar
    assert gen.calls == 2  # generated twice (the at-least-once reality)
    assert _avatar_url(persona) == "avatars/p1.png"  # exactly ONE valid url
    assert len(gen.files) == 1, "deterministic path → re-gen overwrote; NO orphan"


def test_compare_and_set_keeps_exactly_one_url_under_concurrent_redelivery(
    persona: Engine, app_engine: Engine
) -> None:
    # Two runs both generate (different urls) and race to set; the WHERE avatar_url
    # IS NULL compare-and-set must let exactly one win.
    handler = AvatarGenerationHandler(generator=_FakeGenerator())  # type: ignore[arg-type]
    ctx = WorkerJobContext(
        owner_id="user_a", rls_engine=app_engine, job_id="j", job_type=AVATAR_JOB_TYPE
    )

    async def both() -> None:
        await asyncio.gather(
            handler.handle(AvatarGenerationPayload(persona_id="p1"), ctx),
            handler.handle(AvatarGenerationPayload(persona_id="p1"), ctx),
        )

    asyncio.run(both())
    assert _avatar_url(persona) == "avatars/p1.png"  # exactly one, not corrupted
