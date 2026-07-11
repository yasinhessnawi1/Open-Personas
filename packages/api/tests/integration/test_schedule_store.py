"""Integration tests for the RLS-scoped schedule store (Spec A1, T4).

Runs against a real Postgres (``alembic upgrade head`` → migration 013 + RLS) with
the NON-SUPERUSER ``persona_app`` role, so the store's RLS scoping is exercised for
real. Covers: create computes the initial next-fire + audits; CRUD; the ratified
edit rule (recompute next-fire(now), PRESERVE fire_count + created_at — no COUNT
reset); pause preserves / resume recomputes next-fire; record_fire advances +
one-time COMPLETION; delete; exactly ONE AuditEvent per mutation; and the standing
cross-tenant adversarial check (a tenant cannot touch another owner's schedule).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.errors import ScheduleNotFoundError, ScheduleStateError
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule
from persona_api.schedules import ScheduleStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    """The non-superuser ``persona_app`` engine the store runs on (RLS in force).

    Disposed at teardown so no pooled connection lingers into the next test's
    ``migrated_engine`` ``DROP SCHEMA`` (a lingering connection races the drop and
    flakes the batch).
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def store(app_engine: Engine) -> ScheduleStore:
    return ScheduleStore(app_engine)


def _seed_users(superuser_engine: Engine, *user_ids: str) -> None:
    with superuser_engine.begin() as conn:
        for uid in user_ids:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, :e)"),
                {"u": uid, "e": f"{uid}@example.com"},
            )


def _daily(hour: int, *, count: int | None = None) -> RecurrenceRule:
    return RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(hour,), byminute=(0,), count=count)


def _schedule(
    owner_id: str = "user_a",
    *,
    schedule_id: str = "s1",
    rule: RecurrenceRule | None = None,
    one_time_at: datetime | None = None,
) -> Schedule:
    if rule is None and one_time_at is None:
        rule = _daily(7)
    return Schedule(
        id=schedule_id,
        owner_id=owner_id,
        timezone="Europe/Oslo",
        recurrence=rule,
        one_time_at=one_time_at,
        target_job_type="briefing",
        created_at=_NOW,
        updated_at=_NOW,
    )


def _audit_actions(superuser_engine: Engine, target: str) -> list[str]:
    # audit_log.id is a random UUID (not monotonic), so callers must compare
    # order-independently — "exactly one event per mutation" is a multiset claim.
    with superuser_engine.begin() as conn:
        return [
            r.action
            for r in conn.execute(
                text("SELECT action FROM audit_log WHERE target = :t"),
                {"t": target},
            ).all()
        ]


# --- create -----------------------------------------------------------------


def test_create_persists_and_computes_first_fire(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    _seed_users(migrated_engine, "user_a")
    created = store.create(_schedule(), now=_NOW)
    # 07:00 Oslo (CET, +1 in January) on the creation day = 06:00Z.
    assert created.next_fire_at == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    fetched = store.get("user_a", "s1")
    assert fetched.next_fire_at == created.next_fire_at
    assert fetched.recurrence is not None
    assert fetched.recurrence.byhour == (7,)
    assert _audit_actions(migrated_engine, "s1") == ["schedule.create"]


def test_get_missing_raises_not_found(store: ScheduleStore, migrated_engine: Engine) -> None:
    _seed_users(migrated_engine, "user_a")
    with pytest.raises(ScheduleNotFoundError):
        store.get("user_a", "nope")


def test_list_for_owner_returns_only_owner_rows(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    _seed_users(migrated_engine, "user_a", "user_b")
    store.create(_schedule("user_a", schedule_id="a1"), now=_NOW)
    store.create(_schedule("user_a", schedule_id="a2"), now=_NOW)
    store.create(_schedule("user_b", schedule_id="b1"), now=_NOW)
    ids = {s.id for s in store.list_for_owner("user_a")}
    assert ids == {"a1", "a2"}


# --- edit (the ratified rule) -----------------------------------------------


def test_edit_recomputes_next_fire_preserves_count_and_anchor(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    _seed_users(migrated_engine, "user_a")
    store.create(_schedule(rule=_daily(7)), now=_NOW)
    # Simulate two prior fires so fire_count is non-zero (the anti-loophole guard).
    store.record_fire("user_a", "s1", fire_time=datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    store.record_fire("user_a", "s1", fire_time=datetime(2026, 1, 2, 6, 0, tzinfo=UTC))
    before = store.get("user_a", "s1")
    assert before.fire_count == 2

    edit_now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    proposed = before.model_copy(update={"recurrence": _daily(9)})
    edited = store.edit(proposed, now=edit_now)

    assert edited.fire_count == 2  # PRESERVED — no COUNT reset
    assert edited.created_at == _NOW  # anchor stable
    assert edited.recurrence is not None
    assert edited.recurrence.byhour == (9,)  # new rule applied
    # next-fire recomputed from edit_now under the new 09:00 rule: 09:00 CET = 08:00Z.
    assert edited.next_fire_at == datetime(2026, 1, 6, 8, 0, tzinfo=UTC)


def test_edit_cannot_reset_count_via_recreated_anchor(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    # A count-bounded schedule that has exhausted its budget stays exhausted after
    # an edit that only changes the time — created_at is preserved, so the COUNT
    # window does not restart (the loophole the ratified rule closes).
    _seed_users(migrated_engine, "user_a")
    store.create(_schedule(rule=_daily(7, count=1)), now=_NOW)
    store.record_fire("user_a", "s1", fire_time=datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    before = store.get("user_a", "s1")
    assert before.next_fire_at is None  # count=1 already fired → exhausted

    proposed = before.model_copy(update={"recurrence": _daily(9, count=1)})
    edited = store.edit(proposed, now=datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    assert edited.fire_count == 1
    assert edited.next_fire_at is None  # still exhausted — no extra fire from the edit


# --- edit: the R9-023 re-arm guard on a fired one-time -----------------------


def test_edit_rejects_rearm_of_fired_one_time(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    """The origin fix: rescheduling a ONE-TIME that already fired to a new future
    instant must be rejected — never silently re-armed (the R9-023 bug)."""
    _seed_users(migrated_engine, "user_a")
    when = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    store.create(_schedule(one_time_at=when), now=_NOW)
    store.record_fire("user_a", "s1", fire_time=when)
    before = store.get("user_a", "s1")
    assert before.fire_count == 1
    assert before.next_fire_at is None  # one-time COMPLETION

    new_when = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)  # a future re-arm attempt
    proposed = before.model_copy(update={"one_time_at": new_when})
    with pytest.raises(ScheduleStateError, match="already fired"):
        store.edit(proposed, now=datetime(2026, 1, 5, 12, 0, tzinfo=UTC))

    # The row is UNCHANGED — the CAS write never happened (the raise is before it).
    unchanged = store.get("user_a", "s1")
    assert unchanged.next_fire_at is None
    assert unchanged.one_time_at == when  # the OLD fired instant, not the rejected new one
    assert unchanged.fire_count == 1
    assert unchanged.revision == before.revision  # no CAS write landed


def test_edit_allows_non_cadence_change_on_fired_one_time(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    """The guard is scoped to a non-null RE-ARM — editing an unrelated field on a
    fired one-time (its recomputed next_fire_at is still None) must still work."""
    _seed_users(migrated_engine, "user_a")
    when = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    store.create(_schedule(one_time_at=when), now=_NOW)
    store.record_fire("user_a", "s1", fire_time=when)
    before = store.get("user_a", "s1")

    proposed = before.model_copy(update={"target_job_type": "digest"})
    edited = store.edit(proposed, now=datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    assert edited.target_job_type == "digest"
    assert edited.next_fire_at is None  # unchanged — never re-armed
    assert edited.fire_count == 1


# --- pause / resume ---------------------------------------------------------


def test_pause_preserves_next_fire_then_resume_recomputes(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    _seed_users(migrated_engine, "user_a")
    created = store.create(_schedule(), now=_NOW)
    paused = store.pause("user_a", "s1", now=datetime(2026, 1, 1, 1, 0, tzinfo=UTC))
    assert paused.paused is True
    assert paused.next_fire_at == created.next_fire_at  # preserved across pause

    # Resume a week later — next-fire is recomputed from "now", not a stale instant.
    resume_now = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)
    resumed = store.resume("user_a", "s1", now=resume_now)
    assert resumed.paused is False
    assert resumed.next_fire_at == datetime(2026, 1, 9, 6, 0, tzinfo=UTC)  # next 07:00 CET


def test_resume_rejects_rearm_of_a_corrupted_fired_one_time(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    """Defense in depth (R9-023): resume() now routes next_fire_at through the SAME
    model-level guard as edit(), so even a paused, already-fired one-time whose
    ``one_time_at`` was corrupted into the future (the pre-fix zombie shape) cannot
    resume into a re-armed state. This can only be reached via a direct row edit
    (the guard makes it unreachable through any live write path) — proving the
    choke point holds even for a hand-corrupted row, not just the edit door.
    """
    _seed_users(migrated_engine, "user_a")
    when = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    store.create(_schedule(one_time_at=when), now=_NOW)
    store.record_fire("user_a", "s1", fire_time=when)
    store.pause("user_a", "s1", now=datetime(2026, 1, 2, 0, 0, tzinfo=UTC))

    # Simulate the pre-fix corruption directly at the row (bypasses the model guard —
    # the only way to reconstruct this shape now that the guard closes every live path).
    # Must stay AFTER the resume "now" below so next_fire_after recomputes non-null.
    future_one_time = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET one_time_at = :o WHERE id = 's1'"), {"o": future_one_time}
        )

    with pytest.raises(ScheduleStateError, match="already fired"):
        store.resume("user_a", "s1", now=datetime(2026, 6, 2, 0, 0, tzinfo=UTC))
    unchanged = store.get("user_a", "s1")
    assert unchanged.paused is True  # the CAS write never happened
    assert unchanged.next_fire_at is None


# --- record_fire / one-time completion --------------------------------------


def test_record_fire_advances_recurring(migrated_engine: Engine, store: ScheduleStore) -> None:
    _seed_users(migrated_engine, "user_a")
    store.create(_schedule(), now=_NOW)
    fired = store.record_fire("user_a", "s1", fire_time=datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    assert fired.fire_count == 1
    assert fired.last_fire_at == datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    assert fired.next_fire_at == datetime(2026, 1, 2, 6, 0, tzinfo=UTC)  # next day 07:00 CET


def test_record_fire_completes_one_time(migrated_engine: Engine, store: ScheduleStore) -> None:
    _seed_users(migrated_engine, "user_a")
    when = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    store.create(_schedule(rule=None, one_time_at=when), now=_NOW)
    fired = store.record_fire("user_a", "s1", fire_time=when)
    assert fired.fire_count == 1
    assert fired.next_fire_at is None  # one-time COMPLETION
    assert not fired.is_active


# --- heal_zombie_one_time (R9-023 self-heal) ---------------------------------


def _corrupt_into_zombie(engine: Engine, schedule_id: str, *, next_fire_at: datetime) -> None:
    """Force a fired one-time's next_fire_at back to non-null — the pre-fix shape.

    The model/store guard now makes this state unreachable through any live write
    path, so a raw UPDATE is the only way left to reconstruct the exact corrupted
    row shape the self-heal targets (mirrors the dev-DB zombie evidence).
    """
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE schedules SET next_fire_at = :d WHERE id = :i"),
            {"d": next_fire_at, "i": schedule_id},
        )


_HEAL_NOW = datetime(2026, 1, 11, 17, 28, tzinfo=UTC)  # mirrors the R9-023 dev-evidence instant


def test_heal_zombie_one_time_clears_next_fire_and_audits(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    _seed_users(migrated_engine, "user_a")
    when = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    store.create(_schedule(one_time_at=when), now=_NOW)
    store.record_fire("user_a", "s1", fire_time=when)
    zombie_next_fire = datetime(2026, 1, 11, 14, 43, tzinfo=UTC)
    _corrupt_into_zombie(migrated_engine, "s1", next_fire_at=zombie_next_fire)
    zombie = store.get("user_a", "s1")
    assert zombie.next_fire_at == zombie_next_fire  # corrupted shape confirmed
    assert zombie.fire_count == 1

    healed = store.heal_zombie_one_time(
        "user_a", "s1", now=_HEAL_NOW, expected_revision=zombie.revision
    )
    assert healed.next_fire_at is None
    assert healed.fire_count == 1  # untouched — this is not a fire
    assert "schedule.zombie_reconciled" in _audit_actions(migrated_engine, "s1")

    refetched = store.get("user_a", "s1")
    assert refetched.next_fire_at is None


def test_heal_zombie_one_time_is_idempotent_and_ignores_non_zombies(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    """Calling heal on an already-healthy (or already-healed) row is a safe no-op."""
    _seed_users(migrated_engine, "user_a")
    store.create(_schedule(), now=_NOW)  # a normal, never-fired RECURRING schedule
    current = store.get("user_a", "s1")

    result = store.heal_zombie_one_time(
        "user_a", "s1", now=_NOW, expected_revision=current.revision
    )
    assert result == current  # no-op: not a one-time, not a zombie
    assert "schedule.zombie_reconciled" not in _audit_actions(migrated_engine, "s1")


def test_heal_zombie_one_time_handles_the_fire_count_8_shape(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    """Regression for the exact dev-DB evidence shape (fire_count as high as 8)."""
    _seed_users(migrated_engine, "user_a")
    when = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    store.create(_schedule(one_time_at=when), now=_NOW)
    store.record_fire("user_a", "s1", fire_time=when)
    # Bump fire_count to 8 directly (record_fire forbids a second real fire on a
    # one-time by design — this simulates the historical accumulation being healed).
    with migrated_engine.begin() as conn:
        conn.execute(text("UPDATE schedules SET fire_count = 8 WHERE id = 's1'"))
    zombie_next_fire = datetime(2026, 1, 11, 14, 43, tzinfo=UTC)
    _corrupt_into_zombie(migrated_engine, "s1", next_fire_at=zombie_next_fire)
    zombie = store.get("user_a", "s1")
    assert zombie.fire_count == 8

    healed = store.heal_zombie_one_time(
        "user_a", "s1", now=_HEAL_NOW, expected_revision=zombie.revision
    )
    assert healed.next_fire_at is None
    assert healed.fire_count == 8  # preserved — the heal never touches fire_count


# --- delete -----------------------------------------------------------------


def test_delete_removes_then_missing_raises(migrated_engine: Engine, store: ScheduleStore) -> None:
    _seed_users(migrated_engine, "user_a")
    store.create(_schedule(), now=_NOW)
    store.delete("user_a", "s1")
    with pytest.raises(ScheduleNotFoundError):
        store.get("user_a", "s1")
    with pytest.raises(ScheduleNotFoundError):
        store.delete("user_a", "s1")  # second delete → NotFound (idempotent-ish, explicit)


# --- exactly one AuditEvent per mutation ------------------------------------


def test_exactly_one_audit_event_per_mutation(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    _seed_users(migrated_engine, "user_a")
    store.create(_schedule(), now=_NOW)
    store.pause("user_a", "s1", now=_NOW)
    store.resume("user_a", "s1", now=_NOW)
    edited_base = store.get("user_a", "s1")
    store.edit(edited_base.model_copy(update={"target_job_type": "digest"}), now=_NOW)
    store.record_fire("user_a", "s1", fire_time=datetime(2026, 1, 1, 6, 0, tzinfo=UTC))
    store.delete("user_a", "s1")
    actions = sorted(_audit_actions(migrated_engine, "s1"))
    assert actions == sorted(
        [
            "schedule.create",
            "schedule.pause",
            "schedule.resume",
            "schedule.edit",
            "schedule.fire",
            "schedule.delete",
        ]
    )  # exactly one AuditEvent per mutation (six mutations, six rows)


# --- cross-tenant adversarial (the standing RLS check) ----------------------


def test_cross_tenant_cannot_read_edit_pause_or_delete(
    migrated_engine: Engine, store: ScheduleStore
) -> None:
    _seed_users(migrated_engine, "user_a", "user_b")
    store.create(_schedule("user_a", schedule_id="sa"), now=_NOW)

    # user_b cannot SEE user_a's schedule (RLS → NotFound, no oracle).
    with pytest.raises(ScheduleNotFoundError):
        store.get("user_b", "sa")
    with pytest.raises(ScheduleNotFoundError):
        store.pause("user_b", "sa", now=_NOW)
    with pytest.raises(ScheduleNotFoundError):
        store.delete("user_b", "sa")
    with pytest.raises(ScheduleNotFoundError):
        store.edit(_schedule("user_b", schedule_id="sa"), now=_NOW)

    # ...and user_a's schedule is untouched by all those attempts.
    assert store.get("user_a", "sa").id == "sa"
