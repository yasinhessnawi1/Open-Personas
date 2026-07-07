"""Integration tests for the scheduler tick + materialisation (Spec A1, T6).

Real Postgres: the tick claims due schedules cross-tenant (dispatch engine) and
materialises each into an A0 job owner-scoped (RLS engine). Proves:

* **leader-gated** — a non-leader tick is a no-op (claims/materialises nothing);
* **materialisation + handoff (criterion 8)** — a due schedule produces exactly
  one A0 job of the schedule's target type, owned by the schedule's owner, whose
  payload carries the ``schedule_id`` + ``fire_time`` anchor a downstream leg reads;
* **bookkeeping** — fire_count bumps, next_fire advances to a FUTURE instant;
* **effectively-once (criterion 4)** — a crash-rerun of the same due fire yields
  exactly one job per ``(schedule, fire_time)`` (the idempotency key);
* **no burst (structural)** — a long-missed schedule fires ONCE and jumps to the
  next future occurrence, never one-per-tick replay (the grace/skip POLICY + the
  downtime-simulation suite are T7).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.jobs import JobPayload
from persona.schedules import (
    FIRE_PAYLOAD_FIRE_TIME_KEY,
    FIRE_PAYLOAD_SCHEDULE_ID_KEY,
    RecurrenceFreq,
    RecurrenceRule,
    Schedule,
)
from persona_api.schedules import SchedulerLeader, SchedulerTick, ScheduleStore
from pydantic import ConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
_DUE = datetime(2026, 1, 15, 6, 0, tzinfo=UTC)  # <= _NOW → due
_TICK_LOCK_KEY = 0x5C4ED6  # distinct advisory-lock key for the tick tests

# These tests exercise the tick MECHANICS (claim → materialise → bookkeeping →
# no-burst), NOT the missed-fire grace/skip POLICY — that is owned by T7 and its
# own suite (``test_scheduler_missed_fire.py``). With the production default
# grace (≈3h), a ``_DUE`` 6h before ``_NOW`` — and the 10-day-stale fixture in the
# no-burst test — would be SKIPped by ``fire-late-once``, so the mechanics would
# never run. Build the tick with an effectively-unbounded grace so every "due"
# row FIRES; the policy nuance is tested separately. (Production logic unchanged.)
_TEST_GRACE_SECONDS = 366 * 24 * 3600.0  # > any test's lateness → always FIRE_LATE


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()  # don't leave a pooled conn into the next test's DROP SCHEMA


@pytest.fixture
def store(app_engine: Engine) -> ScheduleStore:
    return ScheduleStore(app_engine)


@pytest.fixture
def dispatch_engine(migrated_engine: Engine, database_url: str) -> Iterator[Engine]:
    """A dedicated, per-test superuser engine for the tick's dispatch + the leader.

    NOT the session-scoped ``migrated_engine``: the ``SchedulerLeader`` holds a
    long-lived AUTOCOMMIT advisory-lock connection, which on the shared session
    pool defeats the conftest's stale-connection dispose-guard (sporadic
    ``relation … does not exist``) and can bleed the lock across tests. A fresh
    engine, disposed at teardown, fully isolates the lock + dispatch reads — the
    same per-test-engine pattern ``app_engine`` uses. Depends on ``migrated_engine``
    so the schema is migrated + truncated first.
    """
    engine = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def leader(dispatch_engine: Engine) -> Iterator[SchedulerLeader]:
    lead = SchedulerLeader(dispatch_engine, lock_key=_TICK_LOCK_KEY)
    yield lead
    lead.resign()


@pytest.fixture
def tick(dispatch_engine: Engine, app_engine: Engine, leader: SchedulerLeader) -> SchedulerTick:
    return SchedulerTick(
        dispatch_engine=dispatch_engine,
        rls_engine=app_engine,
        leader=leader,
        default_grace_seconds=_TEST_GRACE_SECONDS,
    )


class _StubLegPayload(JobPayload):
    """A downstream leg's payload — anchors on the handoff fields, ignores the rest.

    Overrides ``extra="ignore"`` (the base ``JobPayload`` forbids extras): a real
    leg reads the schedule anchor and need not model the schedule's arbitrary
    ``payload_template`` keys. This proves typed anchoring on the handoff contract.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    schedule_id: str
    fire_time: str


def _seed_user(engine: Engine, uid: str = "user_a") -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": uid, "e": f"{uid}@example.com"},
        )


def _make_due_schedule(
    superuser_engine: Engine,
    store: ScheduleStore,
    *,
    owner: str = "user_a",
    schedule_id: str = "s1",
    due_at: datetime = _DUE,
    created_at: datetime | None = None,
) -> None:
    """Create a daily-07:00 Oslo schedule, then force its next_fire_at to ``due_at``."""
    created = created_at or datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    store.create(
        Schedule(
            id=schedule_id,
            owner_id=owner,
            timezone="Europe/Oslo",
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,), byminute=(0,)),
            target_job_type="briefing",
            payload_template={"kind": "morning"},
            created_at=created,
            updated_at=created,
        ),
        now=created,
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
                text(
                    "SELECT id, type, owner_id, idempotency_key, payload "
                    "FROM jobs WHERE owner_id = :o"
                ),
                {"o": owner},
            ).mappings()
        ]


# --- leader gating ----------------------------------------------------------


def test_non_leader_tick_is_a_noop(
    migrated_engine: Engine, dispatch_engine: Engine, app_engine: Engine, store: ScheduleStore
) -> None:
    _seed_user(migrated_engine)
    _make_due_schedule(migrated_engine, store)
    # Another process holds leadership; this tick's leader can't acquire. Both
    # run on the dedicated dispatch engine (off the session pool — see the
    # ``dispatch_engine`` fixture), so the held lock connection is isolated.
    holder = SchedulerLeader(dispatch_engine, lock_key=_TICK_LOCK_KEY)
    follower = SchedulerLeader(dispatch_engine, lock_key=_TICK_LOCK_KEY)
    tick = SchedulerTick(dispatch_engine=dispatch_engine, rls_engine=app_engine, leader=follower)
    try:
        assert holder.try_become_leader() is True
        assert tick.run_once(now=_NOW) == 0  # follower → no-op
        assert _jobs_for(migrated_engine, "user_a") == []  # nothing materialised
        assert store.get("user_a", "s1").fire_count == 0  # bookkeeping untouched
    finally:
        holder.resign()
        follower.resign()


# --- materialisation + handoff ----------------------------------------------


def test_due_schedule_materialises_one_job_with_handoff_payload(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine)
    _make_due_schedule(migrated_engine, store)

    assert tick.run_once(now=_NOW) == 1
    jobs = _jobs_for(migrated_engine, "user_a")
    assert len(jobs) == 1
    job = jobs[0]
    assert job["type"] == "briefing"
    assert job["owner_id"] == "user_a"
    assert job["idempotency_key"] == f"sched:s1:{_DUE.isoformat()}"
    # Handoff contract (criterion 8): the anchor is in the payload, template merged.
    assert job["payload"][FIRE_PAYLOAD_SCHEDULE_ID_KEY] == "s1"
    assert job["payload"][FIRE_PAYLOAD_FIRE_TIME_KEY] == _DUE.isoformat()
    assert job["payload"]["kind"] == "morning"  # template preserved
    # A downstream leg can anchor on it (typed consumption).
    anchor = _StubLegPayload.model_validate(job["payload"])
    assert anchor.schedule_id == "s1"
    assert anchor.fire_time == _DUE.isoformat()


def test_owner_autonomy_paused_holds_the_due_schedule(
    migrated_engine: Engine,
    dispatch_engine: Engine,
    app_engine: Engine,
    store: ScheduleStore,
    leader: SchedulerLeader,
) -> None:
    """A6-D-8 completeness: a paused owner's due schedule is HELD — not fired, not advanced.

    The tick is an origination path, so it consults ``is_owner_autonomy_paused`` (here a stub
    that pauses ``user_a``). No job materialises and next_fire stays put, so firing resumes
    cleanly the moment the owner unpauses — no lost fire, no burst, no leaked origination.
    """
    _seed_user(migrated_engine)
    _make_due_schedule(migrated_engine, store)
    paused_tick = SchedulerTick(
        dispatch_engine=dispatch_engine,
        rls_engine=app_engine,
        leader=leader,
        default_grace_seconds=_TEST_GRACE_SECONDS,
        autonomy_pause_check=lambda owner: owner == "user_a",
    )
    assert paused_tick.run_once(now=_NOW) == 0  # held → nothing fired
    assert _jobs_for(migrated_engine, "user_a") == []  # no origination
    held = store.get("user_a", "s1")
    assert held.fire_count == 0  # bookkeeping untouched
    assert held.next_fire_at == _DUE  # not advanced — resumes cleanly on unpause


def test_fire_advances_bookkeeping_to_future(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine)
    _make_due_schedule(migrated_engine, store)
    tick.run_once(now=_NOW)
    fired = store.get("user_a", "s1")
    assert fired.fire_count == 1
    assert fired.last_fire_at == _DUE
    assert fired.next_fire_at is not None
    assert fired.next_fire_at > _NOW  # coalesced to a FUTURE occurrence


def test_not_due_schedule_is_not_fired(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine)
    future = datetime(2026, 2, 1, 6, 0, tzinfo=UTC)
    _make_due_schedule(migrated_engine, store, due_at=future)
    assert tick.run_once(now=_NOW) == 0
    assert _jobs_for(migrated_engine, "user_a") == []


def test_paused_schedule_is_not_fired(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine)
    _make_due_schedule(migrated_engine, store)
    store.pause("user_a", "s1", now=_NOW)
    assert tick.run_once(now=_NOW) == 0
    assert _jobs_for(migrated_engine, "user_a") == []


# --- effectively-once (criterion 4) -----------------------------------------


def test_crash_rerun_of_same_fire_yields_exactly_one_job(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine)
    _make_due_schedule(migrated_engine, store)
    tick.run_once(now=_NOW)  # first tick materialises the fire
    # Simulate a crash-rerun / handover overlap: the SAME (schedule, fire_time) is
    # presented as due again (bookkeeping appeared not to advance). The deterministic
    # key makes the re-materialisation a no-op.
    with migrated_engine.begin() as conn:
        conn.execute(text("UPDATE schedules SET next_fire_at = :d WHERE id = 's1'"), {"d": _DUE})
    tick.run_once(now=_NOW)  # second tick — same fire_time → same key
    jobs = _jobs_for(migrated_engine, "user_a")
    assert len(jobs) == 1  # exactly one job per (schedule, fire_time)


# --- no burst (structural) --------------------------------------------------


def test_long_missed_schedule_fires_once_no_burst(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine)
    # next_fire_at 10 days in the past (worker was "down"): must fire ONCE and jump
    # to the future, NOT replay one occurrence per tick.
    missed = datetime(2026, 1, 5, 6, 0, tzinfo=UTC)
    _make_due_schedule(migrated_engine, store, due_at=missed)
    assert tick.run_once(now=_NOW) == 1
    assert tick.run_once(now=_NOW) == 0  # already jumped to the future — not due again
    jobs = _jobs_for(migrated_engine, "user_a")
    assert len(jobs) == 1  # exactly one fire, no backlog replay
    assert store.get("user_a", "s1").next_fire_at > _NOW


# --- multi-tenant fan-out (cross-tenant claim, owner-scoped materialise) -----


def test_tick_materialises_across_tenants(
    migrated_engine: Engine, store: ScheduleStore, tick: SchedulerTick
) -> None:
    _seed_user(migrated_engine, "user_a")
    _seed_user(migrated_engine, "user_b")
    _make_due_schedule(migrated_engine, store, owner="user_a", schedule_id="sa")
    _make_due_schedule(migrated_engine, store, owner="user_b", schedule_id="sb")
    assert tick.run_once(now=_NOW) == 2  # one global tick fires both owners' due
    assert len(_jobs_for(migrated_engine, "user_a")) == 1
    assert len(_jobs_for(migrated_engine, "user_b")) == 1
