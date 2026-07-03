"""A8 T9 — the calendar edit is the twin of chat: same CAS door, engine preview, quiet parity.

Real Postgres. The calendar-reschedule service is what the ``/v1/me/schedule/{id}/reschedule``
endpoints call. Proves: the preview's next-fire is the ENGINE's (bar 3); apply routes through the
SAME door as chat with ``actor=user_via_ui`` (bar 4 — no second write path); quiet-hours warn+offer
parity in the UI path (bar 5); and RLS (a cross-tenant apply is impossible).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.errors import ScheduleNotFoundError
from persona.schedules import (
    RecurrenceFreq,
    RecurrenceKind,
    RecurrencePattern,
    RecurrenceRule,
    Schedule,
    next_fire_after,
)
from persona_api.schedules import ScheduleStore
from persona_api.services.calendar_reschedule_service import (
    apply_calendar_reschedule,
    preview_calendar_reschedule,
)
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_CREATED = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(migrated_engine: Engine) -> Iterator[Engine]:
    """The ``persona_app`` RLS engine the service runs on (users are seeded via superuser)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    eng = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine: Engine) -> ScheduleStore:
    return ScheduleStore(engine)


def _seed_user(engine: Engine, uid: str, *, quiet: tuple[int, int] | None = None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": uid, "e": f"{uid}@example.com"},
        )
        if quiet is not None:
            conn.execute(
                text("UPDATE users SET quiet_hours_start = :s, quiet_hours_end = :e WHERE id = :u"),
                {"s": quiet[0], "e": quiet[1], "u": uid},
            )


def _create(store: ScheduleStore, owner: str = "user_a", hour: int = 8) -> Schedule:
    return store.create(
        Schedule(
            id="s1",
            owner_id=owner,
            timezone="Europe/Oslo",
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(hour,), byminute=(0,)),
            target_job_type="briefing",
            created_at=_CREATED,
            updated_at=_CREATED,
        ),
        now=_CREATED,
    )


def _daily_pattern(hour: int) -> RecurrencePattern:
    return RecurrencePattern(kind=RecurrenceKind.DAILY, hour=hour, minute=0)


def _reschedule_audit(engine: Engine) -> list[dict[str, object]]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT metadata FROM audit_log "
                    "WHERE target = 's1' AND action = 'schedule.reschedule'"
                )
            )
            .mappings()
            .all()
        ]


def test_preview_next_fire_is_the_engine_computed_instant(
    engine: Engine, migrated_engine: Engine
) -> None:
    """Bar 3: the picker's preview comes from the engine, not client math."""
    _seed_user(migrated_engine, "user_a")
    preview = preview_calendar_reschedule(
        engine,
        owner_id="user_a",
        pattern=_daily_pattern(9),
        one_time_at=None,
        timezone="Europe/Oslo",
        now=_NOW,
    )
    assert "every day at 09:00" in preview.human_terms  # full clause, no raw RRULE
    assert "FREQ=" not in preview.human_terms
    # The preview's next_fire equals an independent engine computation from the same cadence.
    rule = RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(9,), byminute=(0,))
    reference = Schedule(
        id="p",
        owner_id="user_a",
        timezone="Europe/Oslo",
        recurrence=rule,
        target_job_type="preview",
        created_at=_NOW,
        updated_at=_NOW,
    )
    assert preview.next_fire == next_fire_after(reference, after=_NOW)


def test_preview_warns_and_offers_edge_in_quiet_hours(
    engine: Engine, migrated_engine: Engine
) -> None:
    """Bar 5: quiet-hours warn+offer parity in the UI path (22:00–07:00; a 06:00 fire → 07:00)."""
    _seed_user(migrated_engine, "user_a", quiet=(22 * 60, 7 * 60))
    preview = preview_calendar_reschedule(
        engine,
        owner_id="user_a",
        pattern=_daily_pattern(6),
        one_time_at=None,
        timezone="Europe/Oslo",
        now=_NOW,
    )
    assert preview.quiet_hours_offer == "07:00"


def test_preview_no_warn_when_quiet_hours_unset(engine: Engine, migrated_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a")  # off-until-set
    preview = preview_calendar_reschedule(
        engine,
        owner_id="user_a",
        pattern=_daily_pattern(6),
        one_time_at=None,
        timezone="Europe/Oslo",
        now=_NOW,
    )
    assert preview.quiet_hours_offer is None


def test_apply_routes_through_the_same_door_as_chat(
    store: ScheduleStore, engine: Engine, migrated_engine: Engine
) -> None:
    """Bar 4: the calendar edit applies through the ONE CAS door — actor=user_via_ui, audited."""
    _seed_user(migrated_engine, "user_a")
    _create(store, hour=8)
    applied = apply_calendar_reschedule(
        store,
        engine,
        owner_id="user_a",
        schedule_id="s1",
        pattern=_daily_pattern(9),
        one_time_at=None,
        timezone="Europe/Oslo",
        now=_NOW,
    )
    assert applied.recurrence.byhour == (9,)  # type: ignore[union-attr]
    rows = _reschedule_audit(migrated_engine)
    assert len(rows) == 1
    assert rows[0]["metadata"]["actor"] == "user_via_ui"  # the calendar's actor — the same door


def test_apply_is_rls_scoped(store: ScheduleStore, engine: Engine, migrated_engine: Engine) -> None:
    """A cross-tenant calendar apply is impossible (the door's store is owner-scoped)."""
    _seed_user(migrated_engine, "user_a")
    _seed_user(migrated_engine, "user_b")
    _create(store, owner="user_a", hour=8)
    with pytest.raises(ScheduleNotFoundError):
        apply_calendar_reschedule(
            store,
            engine,
            owner_id="user_b",  # B cannot reach A's schedule
            schedule_id="s1",
            pattern=_daily_pattern(9),
            one_time_at=None,
            timezone="Europe/Oslo",
            now=_NOW,
        )
    assert store.get("user_a", "s1").recurrence.byhour == (8,)  # type: ignore[union-attr] — unchanged
