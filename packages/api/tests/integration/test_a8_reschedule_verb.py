"""A8 T6 — the reschedule verb applies through the CAS door + fires for real (bars 4, 7).

Real Postgres + the real ``SchedulerTick``. The runtime half (interpreter + re-echo) is unit-tested;
this proves the api half: ``TaskRescheduleService`` (what the worker calls) resolves the task's
schedule, applies the new cadence through the ONE CAS-guarded door, and — the A4 real-fire bar —
the NEXT REAL FIRE happens at the NEW time through the real scheduler while the OLD slot fires ZERO
times. Skip-next + cross-tenant isolation (RLS) covered too.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from persona.errors import TaskNotFoundError
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule, next_fire_after
from persona_api.schedules import SchedulerLeader, SchedulerTick, ScheduleStore
from persona_api.services.task_reschedule_service import TaskRescheduleService
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_CREATED = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_LOCK = 0x5C4ED8


class _FakeTaskReader:
    """Returns a task whose ``schedule_id`` is the given one — for its owner only (RLS shape)."""

    def __init__(self, schedule_id: str, *, owner: str = "user_a") -> None:
        self._schedule_id = schedule_id
        self._owner = owner

    def get(self, owner_id: str, task_id: str) -> object:  # noqa: ARG002
        if owner_id != self._owner:
            raise TaskNotFoundError("task not found", context={"task_id": task_id})
        return SimpleNamespace(schedule_id=self._schedule_id)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def store(app_engine: Engine) -> ScheduleStore:
    return ScheduleStore(app_engine)


def _seed_user(engine: Engine, uid: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": uid, "e": f"{uid}@example.com"},
        )


def _make(store: ScheduleStore, owner: str, sid: str, hour: int) -> Schedule:
    return store.create(
        Schedule(
            id=sid,
            owner_id=owner,
            timezone="Europe/Oslo",
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(hour,), byminute=(0,)),
            target_job_type="briefing",
            created_at=_CREATED,
            updated_at=_CREATED,
        ),
        now=_CREATED,
    )


def _run_tick(app_engine: Engine, database_url: str, *, now: datetime) -> int:
    dispatch = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    try:
        leader = SchedulerLeader(dispatch, lock_key=_LOCK)
        tick = SchedulerTick(
            dispatch_engine=dispatch,
            rls_engine=app_engine,
            leader=leader,
            default_grace_seconds=366 * 24 * 3600.0,
        )
        fired = tick.run_once(now=now)
        leader.resign()
        return fired
    finally:
        dispatch.dispose()


def test_reschedule_fires_at_new_time_and_old_slot_fires_zero(
    store: ScheduleStore, app_engine: Engine, migrated_engine: Engine, database_url: str
) -> None:
    """The A4 real-fire bar, both directions — through the real scheduler, no hand-invoked step."""
    _seed_user(migrated_engine, "user_a")
    _make(store, "user_a", "s1", 8)  # daily 08:00 Oslo

    svc = TaskRescheduleService(
        task_reader=_FakeTaskReader("s1"), schedule_store=store, engine=app_engine
    )
    # The worker's event payload for "move it to 9" (a user-confirmed reschedule).
    svc.reschedule(
        {
            "owner_id": "user_a",
            "task_id": "task-1",
            "timezone": "Europe/Oslo",
            "recurrence_rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "one_time_at": None,
            "skip_next": False,
        }
    )

    after = store.get("user_a", "s1")
    assert after.recurrence.byhour == (9,)  # type: ignore[union-attr] — applied through the door

    # The authoritative new fire is what the door recomputed (09:00 Oslo); the OLD slot is one
    # hour earlier (the retired 08:00 Oslo mark on the same day).
    new_fire = after.next_fire_at
    assert new_fire is not None
    old_slot = new_fire - timedelta(hours=1)

    # OLD slot: run the real tick at the old time → the schedule is NOT due there → ZERO fires.
    assert _run_tick(app_engine, database_url, now=old_slot) == 0
    assert store.get("user_a", "s1").fire_count == 0
    # NEW time: run the real tick at the new time → it FIRES (real transition, no hand-advance).
    assert _run_tick(app_engine, database_url, now=new_fire) == 1
    fired = store.get("user_a", "s1")
    assert fired.fire_count == 1
    assert fired.last_fire_at == new_fire  # fired at the NEW time, never the old


def test_reschedule_skip_next_via_service(
    store: ScheduleStore, app_engine: Engine, migrated_engine: Engine
) -> None:
    _seed_user(migrated_engine, "user_a")
    _make(store, "user_a", "s1", 8)
    before = store.get("user_a", "s1")
    following = next_fire_after(before, after=before.next_fire_at)  # type: ignore[arg-type]

    svc = TaskRescheduleService(
        task_reader=_FakeTaskReader("s1"), schedule_store=store, engine=app_engine
    )
    svc.reschedule({"owner_id": "user_a", "task_id": "task-1", "skip_next": True})

    after = store.get("user_a", "s1")
    assert after.next_fire_at == following  # the next occurrence was suppressed
    assert after.fire_count == 0


def test_cross_tenant_reschedule_is_impossible(
    store: ScheduleStore, app_engine: Engine, migrated_engine: Engine
) -> None:
    """RLS (bar 7): owner B cannot reschedule owner A's schedule — non-vacuous probe."""
    _seed_user(migrated_engine, "user_a")
    _seed_user(migrated_engine, "user_b")
    _make(store, "user_a", "s1", 8)  # A owns the schedule

    # The reader is scoped to A; a B-owner event resolves no task → the service no-ops.
    svc = TaskRescheduleService(
        task_reader=_FakeTaskReader("s1", owner="user_a"), schedule_store=store, engine=app_engine
    )
    svc.reschedule(
        {
            "owner_id": "user_b",  # a different tenant
            "task_id": "task-1",
            "timezone": "Europe/Oslo",
            "recurrence_rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
            "skip_next": False,
        }
    )
    # A's schedule is untouched (non-vacuous: it DOES still exist at 08:00 for A).
    assert store.get("user_a", "s1").recurrence.byhour == (8,)  # type: ignore[union-attr]
