"""A8 T3 — the mid-flight edit race + the CAS revision guard (A8-D-7, the load-bearing one).

Real Postgres, the ``persona_app`` RLS engine. Proves, against the REAL store CAS path:

* **the interleaving** — an edit landing between a scheduler fire's claim and its re-arm
  fires exactly ONCE and re-arms from the EDITED rule (neither double-fires nor drops the
  occurrence; the tick's stale next-fire is discarded — reconcile-on-miss);
* **I3 (CAS-independent)** — two genuinely concurrent ``apply_fire`` calls for the same due
  fire advance ``fire_count`` exactly ONCE (the ``last_fire_at == fire_time`` idempotency
  guard, independent of the revision mechanism so the fix stays pinned);
* **the door** — ``reschedule`` audits ``old → new`` + actor + provenance, and structurally
  refuses a ``persona_proposed`` direct write (propose-first, A8-D-12).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.errors import OriginationForbiddenError
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule, next_fire_after
from persona_api.schedules import ScheduleStore
from persona_api.schedules.reschedule import RescheduleActor, reschedule
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_CREATED = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
_DUE = datetime(2026, 1, 15, 5, 0, tzinfo=UTC)  # 06:00 Oslo (07:00 CET) — <= _NOW, due


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


def _make_due(store: ScheduleStore, superuser: Engine, *, hour: int = 8) -> Schedule:
    """Create a daily-``hour``:00 Oslo schedule and force it due at ``_DUE``."""
    created = store.create(
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
    with superuser.begin() as conn:
        conn.execute(text("UPDATE schedules SET next_fire_at = :d WHERE id = 's1'"), {"d": _DUE})
    return created


def _audit_rows(superuser: Engine, action: str) -> list[dict[str, object]]:
    with superuser.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT action, metadata FROM audit_log WHERE target = 's1' AND action = :a"),
                {"a": action},
            )
            .mappings()
            .all()
        ]


def test_edit_bumps_revision(store: ScheduleStore, migrated_engine: Engine) -> None:
    _seed_user(migrated_engine)
    store.create(
        Schedule(
            id="s1",
            owner_id="user_a",
            timezone="Europe/Oslo",
            recurrence=_daily(8),
            target_job_type="briefing",
            created_at=_CREATED,
            updated_at=_CREATED,
        ),
        now=_CREATED,
    )
    assert store.get("user_a", "s1").revision == 0
    store.edit(store.get("user_a", "s1").model_copy(update={"recurrence": _daily(9)}), now=_NOW)
    assert store.get("user_a", "s1").revision == 1


def test_mid_flight_edit_between_fire_and_rearm_fires_once_and_preserves_edit(
    store: ScheduleStore, migrated_engine: Engine
) -> None:
    """The load-bearing race: edit lands between claim and re-arm → fire once, edit wins."""
    _seed_user(migrated_engine)
    _make_due(store, migrated_engine, hour=8)

    # The tick CLAIMS the due row (revision 0) and computes the OLD-rule next fire.
    claimed = store.get("user_a", "s1")
    assert claimed.revision == 0
    assert claimed.next_fire_at == _DUE
    tick_old_next = next_fire_after(claimed, after=_NOW)  # from the 08:00 rule

    # An EDIT lands (08:00 → 09:00) between the claim and the re-arm.
    store.edit(claimed.model_copy(update={"recurrence": _daily(9)}), now=_NOW)
    edited = store.get("user_a", "s1")
    assert edited.revision == 1
    edited_next = next_fire_after(edited, after=_NOW)  # from the 09:00 rule
    assert edited_next != tick_old_next  # the two rules diverge (distinguishable)

    # The tick re-arms with its STALE claimed revision (0) + its OLD-rule next fire.
    store.apply_fire(
        "user_a",
        "s1",
        fire_time=_DUE,
        next_fire_at=tick_old_next,
        now=_NOW,
        expected_revision=claimed.revision,  # 0 — stale, the edit bumped it to 1
    )

    after = store.get("user_a", "s1")
    assert after.fire_count == 1  # fired exactly ONCE (no drop, no double)
    assert after.last_fire_at == _DUE  # the claimed occurrence was recorded
    assert after.next_fire_at == edited_next  # re-armed from the EDITED rule, not the stale one
    assert after.next_fire_at != tick_old_next  # the tick's stale next-fire was discarded
    assert after.revision == 2  # edit (0→1) + reconciled fire (1→2)


def test_two_concurrent_apply_fire_advance_exactly_once(
    store: ScheduleStore, app_engine: Engine, migrated_engine: Engine
) -> None:
    """I3 (CAS-independent): two concurrent ticks re-arming the same fire ⇒ one advance."""
    _seed_user(migrated_engine)
    _make_due(store, migrated_engine, hour=8)
    claimed = store.get("user_a", "s1")
    next_fire = next_fire_after(claimed, after=_NOW)

    app_url = os.environ["APP_DATABASE_URL"].replace("+asyncpg", "+psycopg")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _fire() -> None:
        engine = create_engine(app_url)
        try:
            thread_store = ScheduleStore(engine)
            barrier.wait()  # release both threads at once → genuine contention
            thread_store.apply_fire(
                "user_a",
                "s1",
                fire_time=_DUE,
                next_fire_at=next_fire,
                now=_NOW,
                expected_revision=claimed.revision,
            )
        except BaseException as exc:  # noqa: BLE001 — surface a thread failure to the test
            errors.append(exc)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=_fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"a concurrent apply_fire raised: {errors}"
    assert store.get("user_a", "s1").fire_count == 1  # EXACTLY one advance
    assert len(_audit_rows(migrated_engine, "schedule.fire")) == 1  # one durable fire note


def test_reschedule_door_audits_old_new_actor(
    store: ScheduleStore, migrated_engine: Engine
) -> None:
    _seed_user(migrated_engine)
    _make_due(store, migrated_engine, hour=8)
    current = store.get("user_a", "s1")

    after = reschedule(
        store,
        migrated_engine,
        owner_id="user_a",
        schedule_id="s1",
        new_schedule=current.model_copy(update={"recurrence": _daily(9)}),
        actor=RescheduleActor.USER_VIA_UI,
        provenance="calendar time-picker edit",
        now=_NOW,
    )
    assert after.recurrence is not None
    assert after.recurrence.byhour == (9,)

    rows = _audit_rows(migrated_engine, "schedule.reschedule")
    assert len(rows) == 1
    meta = rows[0]["metadata"]
    assert meta["actor"] == "user_via_ui"
    assert meta["provenance"] == "calendar time-picker edit"
    assert "08:00" in meta["old"]  # the old cadence, human terms
    assert "09:00" in meta["new"]  # the new cadence, human terms


def test_reschedule_door_refuses_persona_proposed(
    store: ScheduleStore, migrated_engine: Engine
) -> None:
    """Propose-first structural (A8-D-12): a persona actor can't apply directly (T4 wires it)."""
    _seed_user(migrated_engine)
    _make_due(store, migrated_engine, hour=8)
    current = store.get("user_a", "s1")

    with pytest.raises(OriginationForbiddenError):
        reschedule(
            store,
            migrated_engine,
            owner_id="user_a",
            schedule_id="s1",
            new_schedule=current.model_copy(update={"recurrence": _daily(9)}),
            actor=RescheduleActor.PERSONA_PROPOSED,
            provenance="persona noticed a quiet-hours collision",
            now=_NOW,
        )
    # Nothing applied, nothing audited — the refusal is before any write.
    assert store.get("user_a", "s1").recurrence.byhour == (8,)  # type: ignore[union-attr]
    assert _audit_rows(migrated_engine, "schedule.reschedule") == []
