"""R9-023 — the tick self-heals a one-time schedule wrongly re-armed after it fired.

Real Postgres + the real ``SchedulerTick``. Before this fix, a one-time whose
``next_fire_at`` was corrupted back to non-null after it had already fired
(``fire_count > 0``) was claimed as due on EVERY tick, and ``record_fire`` raised
``ScheduleStateError("one-time schedule already fired")`` every time — caught by
``run_once``'s per-schedule try/except, logged, and skipped, with nothing
advancing the row: an infinite, silent error-skip loop (a live zombie row).

Proves: the tick detects the zombie shape BEFORE the missed-fire policy runs,
reconciles it terminal (``next_fire_at`` -> ``NULL``) through the real
``ScheduleStore.heal_zombie_one_time``, audits ``schedule.zombie_reconciled``,
logs exactly one WARNING naming the schedule, never enqueues a job (no re-fire —
the historical exactly-once violation this closes), and — because the row is no
longer due once healed — is silent on every subsequent tick. Also proves the
boundary: a NEVER-fired one-time and a normal RECURRING schedule (both of which
can have ``fire_count`` unrelated-to-zero in the recurring case) are never
mistaken for a zombie and fire exactly as before.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from loguru import logger as _loguru_logger
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule
from persona_api.schedules import SchedulerLeader, SchedulerTick, ScheduleStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_CREATED = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_LOCK_KEY = 0x5C4ED9
# The dev-evidence instant shape (R9-023 issue log): re-armed same-day, now overdue.
_ZOMBIE_DUE_AT = datetime(2026, 7, 11, 14, 43, tzinfo=UTC)
_NOW = datetime(2026, 7, 11, 17, 28, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def store(app_engine: Engine) -> ScheduleStore:
    return ScheduleStore(app_engine)


@pytest.fixture
def dispatch_engine(migrated_engine: Engine, database_url: str) -> Iterator[Engine]:
    """A dedicated, per-test superuser engine for the tick's dispatch + the leader
    (off the shared session pool — see ``test_scheduler_tick.py``'s fixture for why)."""
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def leader(dispatch_engine: Engine) -> Iterator[SchedulerLeader]:
    lead = SchedulerLeader(dispatch_engine, lock_key=_LOCK_KEY)
    yield lead
    lead.resign()


@pytest.fixture
def tick(dispatch_engine: Engine, app_engine: Engine, leader: SchedulerLeader) -> SchedulerTick:
    return SchedulerTick(
        dispatch_engine=dispatch_engine,
        rls_engine=app_engine,
        leader=leader,
        default_grace_seconds=366 * 24 * 3600.0,
        one_time_grace_seconds=366 * 24 * 3600.0,
    )


def _seed_user(engine: Engine, uid: str = "user_a") -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": uid, "e": f"{uid}@example.com"},
        )


def _make_zombie_one_time(
    superuser_engine: Engine,
    store: ScheduleStore,
    *,
    owner: str = "user_a",
    schedule_id: str = "s1",
    fire_count: int = 1,
    due_at: datetime = _ZOMBIE_DUE_AT,
) -> None:
    """Seed the exact corrupted shape the R9-023 evidence describes.

    A one-time schedule that legitimately fired once (``record_fire`` — this is
    the ONLY code path that ever sets ``fire_count``), then had its
    ``next_fire_at`` forced back to a non-null, now-overdue instant directly at
    the row — the model/store guard (:meth:`Schedule.with_next_fire`) makes this
    unreachable through any live write path post-fix, so a raw UPDATE is the only
    way left to reconstruct it (mirrors ``fire_count`` up to 8 in the dev evidence
    via a second raw bump when the caller wants that shape).
    """
    fire_at = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    store.create(
        Schedule(
            id=schedule_id,
            owner_id=owner,
            timezone="Europe/Oslo",
            one_time_at=fire_at,
            target_job_type="briefing",
            created_at=_CREATED,
            updated_at=_CREATED,
        ),
        now=_CREATED,
    )
    store.record_fire(owner, schedule_id, fire_time=fire_at)
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET next_fire_at = :d, fire_count = :fc WHERE id = :i"),
            {"d": due_at, "fc": fire_count, "i": schedule_id},
        )


def _make_due_recurring(
    superuser_engine: Engine,
    store: ScheduleStore,
    *,
    owner: str = "user_a",
    schedule_id: str = "s1",
    due_at: datetime,
) -> None:
    store.create(
        Schedule(
            id=schedule_id,
            owner_id=owner,
            timezone="Europe/Oslo",
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,), byminute=(0,)),
            target_job_type="briefing",
            created_at=_CREATED,
            updated_at=_CREATED,
        ),
        now=_CREATED,
    )
    with superuser_engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET next_fire_at = :d WHERE id = :i"),
            {"d": due_at, "i": schedule_id},
        )


def _jobs_for(engine: Engine, owner: str) -> list[dict]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT id FROM jobs WHERE owner_id = :o"), {"o": owner}
            ).mappings()
        ]


def _audit_actions(engine: Engine, target: str = "s1") -> list[str]:
    with engine.begin() as conn:
        return [
            r.action
            for r in conn.execute(
                text("SELECT action FROM audit_log WHERE target = :t"), {"t": target}
            ).all()
        ]


def _run_tick_capturing_warnings(tick: SchedulerTick, *, now: datetime) -> tuple[int, list[str]]:
    """Run one tick with a temporary loguru sink capturing WARNING+ records."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(captured.append, level="WARNING", format="{message} | {extra}")
    try:
        fired = tick.run_once(now=now)
    finally:
        _loguru_logger.remove(sink_id)
    return fired, captured


# --- the self-heal ------------------------------------------------------------


def test_zombie_one_time_heals_on_first_tick(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine)
    _make_zombie_one_time(migrated_engine, store)
    zombie = store.get("user_a", "s1")
    assert zombie.next_fire_at == _ZOMBIE_DUE_AT  # corrupted shape confirmed
    assert zombie.fire_count == 1

    fired = tick.run_once(now=_NOW)

    assert fired == 0  # healed, not fired — no origination
    healed = store.get("user_a", "s1")
    assert healed.next_fire_at is None
    assert healed.fire_count == 1  # untouched — the heal is not a fire
    assert _jobs_for(migrated_engine, "user_a") == []  # no re-fire (the exactly-once bar)
    assert "schedule.zombie_reconciled" in _audit_actions(migrated_engine)


def test_zombie_heal_logs_exactly_one_warning_naming_the_schedule(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine)
    _make_zombie_one_time(migrated_engine, store)

    fired, captured = _run_tick_capturing_warnings(tick, now=_NOW)

    assert fired == 0
    zombie_warnings = [line for line in captured if "zombie" in line.lower()]
    assert len(zombie_warnings) == 1  # exactly one WARNING for this occurrence
    assert "s1" in zombie_warnings[0]  # names the schedule_id


def test_healed_zombie_is_not_reclaimed_on_next_tick(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine)
    _make_zombie_one_time(migrated_engine, store)
    tick.run_once(now=_NOW)  # first tick heals it

    fired, captured = _run_tick_capturing_warnings(tick, now=_NOW + timedelta(hours=1))

    assert fired == 0
    assert not [line for line in captured if "zombie" in line.lower()]  # silent — not claimed
    assert _audit_actions(migrated_engine).count("schedule.zombie_reconciled") == 1  # not repeated


def test_zombie_with_fire_count_8_also_heals(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    """Regression for the exact dev-DB evidence shape (fire_count as high as 8)."""
    _seed_user(migrated_engine)
    _make_zombie_one_time(migrated_engine, store, fire_count=8)
    assert store.get("user_a", "s1").fire_count == 8

    fired = tick.run_once(now=_NOW)

    assert fired == 0
    healed = store.get("user_a", "s1")
    assert healed.next_fire_at is None
    assert healed.fire_count == 8  # preserved, never touched by the heal
    assert _jobs_for(migrated_engine, "user_a") == []


def test_zombie_heal_is_owner_scoped_among_multiple_due_schedules(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    """A zombie for one owner never blocks or bleeds into another owner's due fire."""
    _seed_user(migrated_engine, "user_a")
    _seed_user(migrated_engine, "user_b")
    _make_zombie_one_time(migrated_engine, store, owner="user_a", schedule_id="zombie")
    _make_due_recurring(migrated_engine, store, owner="user_b", schedule_id="healthy", due_at=_NOW)

    fired = tick.run_once(now=_NOW)

    assert fired == 1  # only the healthy owner_b fire counts as fired
    assert store.get("user_a", "zombie").next_fire_at is None  # healed
    assert store.get("user_b", "healthy").fire_count == 1  # fired normally
    assert _jobs_for(migrated_engine, "user_a") == []
    assert len(_jobs_for(migrated_engine, "user_b")) == 1


# --- the boundary: never mistake a healthy schedule for a zombie --------------


def test_never_fired_one_time_due_schedule_still_fires_normally(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    """fire_count == 0 is never a zombie, even though it's a due one-time."""
    _seed_user(migrated_engine)
    store.create(
        Schedule(
            id="s1",
            owner_id="user_a",
            timezone="Europe/Oslo",
            one_time_at=_NOW - timedelta(minutes=5),
            target_job_type="briefing",
            created_at=_CREATED,
            updated_at=_CREATED,
        ),
        now=_CREATED,
    )
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET next_fire_at = :d WHERE id = 's1'"),
            {"d": _NOW - timedelta(minutes=5)},
        )

    fired = tick.run_once(now=_NOW)

    assert fired == 1  # a REAL fire, not a heal
    done = store.get("user_a", "s1")
    assert done.fire_count == 1
    assert done.next_fire_at is None  # one-time COMPLETION
    assert len(_jobs_for(migrated_engine, "user_a")) == 1


def test_recurring_schedule_with_fire_count_still_fires_normally(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    """A RECURRING schedule's normal fire_count > 0 must never trip the one-time-only guard."""
    _seed_user(migrated_engine)
    _make_due_recurring(migrated_engine, store, due_at=_NOW - timedelta(days=1))
    tick.run_once(now=_NOW)  # first fire — fire_count becomes 1
    after_first = store.get("user_a", "s1")
    assert after_first.fire_count == 1
    assert after_first.next_fire_at is not None  # a live recurring next-fire, not zombie shape

    # Force it due again (simulating the next day's occurrence coming due for real).
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET next_fire_at = :d WHERE id = 's1'"),
            {"d": _NOW - timedelta(minutes=1)},
        )

    fired = tick.run_once(now=_NOW)

    assert fired == 1  # fires again normally — recurring, not a zombie
    twice_fired = store.get("user_a", "s1")
    assert twice_fired.fire_count == 2
    assert twice_fired.next_fire_at is not None
    assert len(_jobs_for(migrated_engine, "user_a")) == 2
