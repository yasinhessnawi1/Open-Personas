"""A8 T4 — propose-first structural gate + skip-next on the real scheduler (A8-D-12/D-2).

Real Postgres, ``persona_app`` RLS engine + the real ``SchedulerTick``. Proves:

* **propose-first is structural** — no code path applies a persona-originated change without a
  user-confirmation reference: the direct door refuses ``persona_proposed``; ``propose_reschedule``
  writes NOTHING; ``apply_proposal`` refuses anything not ``confirmed``; only a user-resolved
  (``resolved_by``) proposal applies, and it applies as ``user_via_chat``;
* **skip-next on the real scheduler** — suppresses exactly the next occurrence (audited), and the
  FOLLOWING occurrence still fires through the real tick (real-transition, not asserted).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.errors import OriginationForbiddenError
from persona.schedules import (
    RecurrenceFreq,
    RecurrenceRule,
    RescheduleProposalStatus,
    Schedule,
    next_fire_after,
    resolve_proposal,
)
from persona_api.schedules import SchedulerLeader, SchedulerTick, ScheduleStore
from persona_api.schedules.reschedule import (
    RescheduleActor,
    apply_proposal,
    propose_reschedule,
    reschedule,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_CREATED = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
_PROPOSE_LOCK_KEY = 0x5C4ED7


def _daily(hour: int) -> RecurrenceRule:
    return RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(hour,), byminute=(0,))


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


def _seed_user(engine: Engine, uid: str = "user_a") -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": uid, "e": f"{uid}@example.com"},
        )


def _create(store: ScheduleStore, *, hour: int = 8) -> Schedule:
    return store.create(
        Schedule(
            id="s1",
            owner_id="user_a",
            timezone="Europe/Oslo",
            recurrence=_daily(hour),
            target_job_type="briefing",
            created_at=_CREATED,
            updated_at=_CREATED,
        ),
        now=_CREATED,
    )


def _audit_meta(engine: Engine, action: str) -> list[dict[str, object]]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT metadata FROM audit_log WHERE target = 's1' AND action = :a"),
                {"a": action},
            )
            .mappings()
            .all()
        ]


def _reschedule_audit(engine: Engine) -> list[dict[str, object]]:
    return _audit_meta(engine, "schedule.reschedule")


# --- propose-first is structural (A8-D-12) --------------------------------------------------


def test_propose_reschedule_writes_nothing(store: ScheduleStore, migrated_engine: Engine) -> None:
    _seed_user(migrated_engine)
    _create(store, hour=8)
    proposal = propose_reschedule(
        store,
        proposal_id="p1",
        owner_id="user_a",
        schedule_id="s1",
        persona_id="persona_1",
        new_recurrence=_daily(9),
        new_one_time=None,
        new_timezone="Europe/Oslo",
        reason="the 08:00 run keeps hitting your quiet hours",
        now=_NOW,
    )
    assert proposal.status is RescheduleProposalStatus.PENDING
    assert "09:00" in proposal.human_terms
    # NOTHING applied: the schedule is untouched, no reschedule audit row.
    assert store.get("user_a", "s1").recurrence.byhour == (8,)  # type: ignore[union-attr]
    assert _reschedule_audit(migrated_engine) == []


def test_apply_proposal_refuses_unconfirmed(store: ScheduleStore, migrated_engine: Engine) -> None:
    _seed_user(migrated_engine)
    _create(store, hour=8)
    pending = propose_reschedule(
        store,
        proposal_id="p1",
        owner_id="user_a",
        schedule_id="s1",
        persona_id="persona_1",
        new_recurrence=_daily(9),
        new_one_time=None,
        new_timezone="Europe/Oslo",
        reason="quiet-hours collision",
        now=_NOW,
    )
    # A PENDING proposal cannot be applied (structural: no un-confirmed persona change applies).
    with pytest.raises(OriginationForbiddenError):
        apply_proposal(store, migrated_engine, pending, now=_NOW)
    # A REJECTED proposal cannot be applied either.
    rejected = resolve_proposal(pending, approve=False, resolved_by="user_a", now=_NOW)
    with pytest.raises(OriginationForbiddenError):
        apply_proposal(store, migrated_engine, rejected, now=_NOW)
    assert store.get("user_a", "s1").recurrence.byhour == (8,)  # type: ignore[union-attr]
    assert _reschedule_audit(migrated_engine) == []


def test_confirmed_proposal_applies_as_user_via_chat(
    store: ScheduleStore, migrated_engine: Engine
) -> None:
    """The full propose→confirm→apply chain: applies only after a user resolves it."""
    _seed_user(migrated_engine)
    _create(store, hour=8)
    pending = propose_reschedule(
        store,
        proposal_id="p1",
        owner_id="user_a",
        schedule_id="s1",
        persona_id="persona_1",
        new_recurrence=_daily(9),
        new_one_time=None,
        new_timezone="Europe/Oslo",
        reason="quiet-hours collision",
        now=_NOW,
    )
    confirmed = resolve_proposal(pending, approve=True, resolved_by="user_a", now=_NOW)
    after = apply_proposal(store, migrated_engine, confirmed, now=_NOW)

    assert after.recurrence.byhour == (9,)  # type: ignore[union-attr] — applied
    rows = _reschedule_audit(migrated_engine)
    assert len(rows) == 1
    meta = rows[0]["metadata"]
    assert meta["actor"] == "user_via_chat"  # a user confirmed it in conversation
    assert "p1" in meta["provenance"]  # the confirmation reference is threaded through
    assert "user_a" in meta["provenance"]


def test_direct_door_still_refuses_persona_proposed(
    store: ScheduleStore, migrated_engine: Engine
) -> None:
    """The one door never applies a raw persona actor — the only persona path is propose→confirm."""
    _seed_user(migrated_engine)
    current = _create(store, hour=8)
    with pytest.raises(OriginationForbiddenError):
        reschedule(
            store,
            migrated_engine,
            owner_id="user_a",
            schedule_id="s1",
            new_schedule=current.model_copy(update={"recurrence": _daily(9)}),
            actor=RescheduleActor.PERSONA_PROPOSED,
            provenance="persona wants it",
            now=_NOW,
        )


# --- skip-next on the real scheduler (A8-D-2) -----------------------------------------------


def test_skip_next_suppresses_next_and_following_still_fires(
    store: ScheduleStore, migrated_engine: Engine, database_url: str
) -> None:
    """Skip-next suppresses exactly the next occurrence; the FOLLOWING fires on the real tick."""
    _seed_user(migrated_engine)
    _create(store, hour=8)
    # Force the next fire to a concrete due instant (08:00 Oslo = 07:00Z on 2026-01-16).
    day1 = datetime(2026, 1, 16, 7, 0, tzinfo=UTC)
    with migrated_engine.begin() as conn:
        conn.execute(text("UPDATE schedules SET next_fire_at = :d WHERE id = 's1'"), {"d": day1})

    before = store.get("user_a", "s1")
    day2 = next_fire_after(before, after=day1)  # the FOLLOWING occurrence (08:00 Oslo, next day)

    skipped = store.skip_next("user_a", "s1", now=datetime(2026, 1, 16, 0, 0, tzinfo=UTC))
    assert skipped.next_fire_at == day2  # advanced past day1 to day2, WITHOUT firing
    assert skipped.fire_count == 0  # no fire happened — the occurrence was suppressed
    # Audit records the suppressed instant.
    rows = _audit_meta(migrated_engine, "schedule.skip_next")
    assert len(rows) == 1
    assert rows[0]["metadata"]["suppressed_fire_time"] == day1.isoformat()

    # The FOLLOWING occurrence is unaffected — the real tick fires it (real-transition).
    dispatch = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    app = create_engine(os.environ["APP_DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    try:
        leader = SchedulerLeader(dispatch, lock_key=_PROPOSE_LOCK_KEY)
        tick = SchedulerTick(
            dispatch_engine=dispatch,
            rls_engine=app,
            leader=leader,
            default_grace_seconds=366 * 24 * 3600.0,  # always FIRE (mechanics, not policy)
        )
        fired = tick.run_once(now=day2)  # at day2 the following occurrence is due
        leader.resign()
        assert fired == 1  # the following occurrence fired
    finally:
        dispatch.dispose()
        app.dispose()

    after = store.get("user_a", "s1")
    assert after.fire_count == 1  # exactly the following occurrence fired
    assert after.last_fire_at == day2  # day1 never fired; day2 did
