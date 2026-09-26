"""R9-237: the quiet-hours deferred flush through the REAL A0 queue and executor.

The unit suite proves the pipeline holds inside quiet hours and the handler's
gates. This file proves the durable half on Postgres: two scan fires of the same
morning asking for the same release instant leave ONE job row (the A0
``(owner_id, idempotency_key)`` unique), the job is not claimable before the
window ends and is claimable at it, and the real executor rebuilds the payload
from JSONB and runs the handler under the owner.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.initiative import InitiativeDial
from persona.jobs import JobRegistry, JobState
from persona_api.initiative.deferred_flush import (
    INITIATIVE_DEFERRED_FLUSH_JOB_TYPE,
    InitiativeDeferredFlushHandler,
    QueueDeferredFlushScheduler,
    register_initiative_deferred_flush_handler,
)
from persona_api.jobs.executor import JobExecutor
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "own_r9237"
# 08:00 Oslo (CEST) on 4 Jul 2026: the end of a 22:00 to 08:00 window. In the past, so
# the final claim runs on the real clock and the executor's lease is a live one.
_RELEASE = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001, orders the schema first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


class _RecordingFlush:
    def __init__(self) -> None:
        self.owners: list[str] = []

    async def flush(self, owner_id: str) -> None:
        self.owners.append(owner_id)


def test_one_job_per_owner_per_release_claimable_only_at_the_window_end(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )

    scheduler = QueueDeferredFlushScheduler(JobQueue(app_engine))
    scheduler.schedule_flush(_OWNER, release_at=_RELEASE)  # persona A's 07:00 fire
    scheduler.schedule_flush(_OWNER, release_at=_RELEASE)  # persona B's 07:00 fire

    with migrated_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT scheduled_at FROM jobs WHERE owner_id = :u AND type = :t"),
            {"u": _OWNER, "t": INITIATIVE_DEFERRED_FLUSH_JOB_TYPE},
        ).all()
    assert len(rows) == 1
    assert rows[0][0] == _RELEASE

    dispatch = JobQueue(migrated_engine)
    early = dispatch.claim(
        worker_id="w-r9237", lease_seconds=60, now=_RELEASE - timedelta(minutes=1)
    )
    assert early == []  # still inside quiet hours: not due
    records = dispatch.claim(worker_id="w-r9237", lease_seconds=60)
    assert [r.type for r in records] == [INITIATIVE_DEFERRED_FLUSH_JOB_TYPE]

    flush = _RecordingFlush()
    registry = JobRegistry()
    register_initiative_deferred_flush_handler(
        registry,
        handler=InitiativeDeferredFlushHandler(
            held_batch=flush,
            held_personas=lambda _owner: {"p1"},
            dial_reader=lambda _o, _p: InitiativeDial.PROPOSE_ONLY,
        ),
    )
    executor = JobExecutor(
        queue=dispatch, registry=registry, rls_engine=app_engine, worker_id="w-r9237"
    )
    assert asyncio.run(executor.execute(records[0])) is JobState.SUCCEEDED
    assert flush.owners == [_OWNER]
