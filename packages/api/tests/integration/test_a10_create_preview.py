"""A10 T2 — the create preview: the engine's truth, no write, quiet-hours parity.

Real Postgres. ``POST /v1/me/schedule/preview`` calls the SAME shared engine preview
(``preview_schedule_cadence`` — the D-1 rename of A8's schedule-independent
``preview_calendar_reschedule``, proven an alias, not a copy). Proves: the preview's
next-fire equals an independent engine walk (the same-engine guarantee — the whole point
of the preview existing); the full tz-framed clause carries no raw RRULE; NO write
happens; and a create landing in quiet hours warns + offers the nearest edge on BOTH the
preview and the create result (criterion 7's warn half).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from persona.schedules import (
    QuietHours,
    RecurrenceKind,
    RecurrencePattern,
    Schedule,
    next_fire_after,
    occurrences_between,
    pattern_to_rule,
    quiet_hours_edge,
)
from persona_api.schedules import ScheduleStore
from persona_api.services.calendar_reschedule_service import (
    preview_calendar_reschedule,
    preview_schedule_cadence,
)
from persona_api.services.schedule_create_service import create_user_schedule
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
_TZ = "Europe/Oslo"


@pytest.fixture
def engine(migrated_engine: Engine) -> Iterator[Engine]:
    """The ``persona_app`` RLS engine the preview runs on (seeding rides the superuser)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    eng = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield eng
    eng.dispose()


def _seed_user(
    seed: Engine, uid: str, *, persona_id: str | None = None, quiet: tuple[int, int] | None = None
) -> None:
    with seed.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": uid, "e": f"{uid}@example.com"},
        )
        if persona_id is not None:
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
                {"p": persona_id, "o": uid},
            )
        if quiet is not None:
            conn.execute(
                text("UPDATE users SET quiet_hours_start = :s, quiet_hours_end = :e WHERE id = :u"),
                {"s": quiet[0], "e": quiet[1], "u": uid},
            )


def _daily(hour: int) -> RecurrencePattern:
    return RecurrencePattern(kind=RecurrenceKind.DAILY, hour=hour, minute=0)


def _reference(pattern: RecurrencePattern) -> Schedule:
    """An independent Schedule for the engine walk the preview must equal."""
    return Schedule(
        id="ref",
        owner_id="user_a",
        timezone=_TZ,
        recurrence=pattern_to_rule(pattern),
        target_job_type="ref",
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_the_shared_preview_is_an_alias_not_a_copy() -> None:
    """D-1: one engine preview serves both twins — the same function object."""
    assert preview_schedule_cadence is preview_calendar_reschedule


def test_preview_next_fire_equals_an_independent_engine_walk(
    engine: Engine, migrated_engine: Engine
) -> None:
    """The same-engine guarantee: preview == next_fire_after == the occurrences walk."""
    _seed_user(migrated_engine, "user_a")
    pattern = _daily(9)
    preview = preview_schedule_cadence(
        engine, owner_id="user_a", pattern=pattern, one_time_at=None, timezone=_TZ, now=_NOW
    )
    reference = _reference(pattern)
    assert preview.next_fire == next_fire_after(reference, after=_NOW)
    # And the forward-expansion engine agrees: the preview's next fire is the FIRST
    # occurrence the calendar's own read would render for this cadence.
    walk = occurrences_between(reference, _NOW, _NOW + timedelta(days=3), cap=10)
    assert preview.next_fire == walk[0]
    assert "every day at 09:00" in preview.human_terms
    assert "FREQ=" not in preview.human_terms  # tz-framed human clause, never raw RRULE
    assert preview.timezone == _TZ


def test_one_time_preview_echoes_the_instant(engine: Engine, migrated_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a")
    at = _NOW + timedelta(days=2)
    preview = preview_schedule_cadence(
        engine, owner_id="user_a", pattern=None, one_time_at=at, timezone=_TZ, now=_NOW
    )
    assert preview.next_fire == at
    assert preview.quiet_hours_offer is None  # no quiet window set — no offer


def test_preview_writes_nothing(engine: Engine, migrated_engine: Engine) -> None:
    """NO write: previewing is reading the engine, never touching the schedules table."""
    _seed_user(migrated_engine, "user_a")
    preview_schedule_cadence(
        engine, owner_id="user_a", pattern=_daily(9), one_time_at=None, timezone=_TZ, now=_NOW
    )
    assert ScheduleStore(engine).list_for_owner("user_a") == []
    with migrated_engine.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM schedules")).scalar()
    assert count == 0


def test_quiet_hours_warn_and_offer_on_preview_and_create(
    engine: Engine, migrated_engine: Engine
) -> None:
    """Criterion 7 (warn half): a cadence inside the quiet window offers the nearest edge —
    identically on the preview and on the create's confirmation echo; the create is NOT
    blocked (the user may accept the edge or override — never a silent shift)."""
    quiet = (8 * 60, 10 * 60)  # 08:00–10:00 in the user's tz
    _seed_user(migrated_engine, "user_a", persona_id="pa", quiet=quiet)
    pattern = _daily(9)  # 09:00 local — inside the window

    preview = preview_schedule_cadence(
        engine, owner_id="user_a", pattern=pattern, one_time_at=None, timezone=_TZ, now=_NOW
    )
    assert preview.next_fire is not None
    local = preview.next_fire.astimezone(ZoneInfo(_TZ))
    expected_edge = quiet_hours_edge(
        local.hour * 60 + local.minute, QuietHours(start_minute=quiet[0], end_minute=quiet[1])
    )
    assert expected_edge is not None
    assert preview.quiet_hours_offer == f"{expected_edge // 60:02d}:{expected_edge % 60:02d}"

    created = create_user_schedule(
        engine,
        ScheduleStore(engine),
        TaskStore(engine),
        owner_id="user_a",
        pattern=pattern,
        one_time_at=None,
        timezone=_TZ,
        persona_id="pa",
        subject="quiet-hours reminder",
        idempotency_key="dialog-quiet-01",
        now=_NOW,
    )
    assert created.created is True  # warned, offered — never blocked, never shifted
    assert created.quiet_hours_offer == preview.quiet_hours_offer


def test_outside_quiet_hours_no_offer(engine: Engine, migrated_engine: Engine) -> None:
    _seed_user(migrated_engine, "user_a", quiet=(0, 6 * 60))  # 00:00–06:00
    preview = preview_schedule_cadence(
        engine, owner_id="user_a", pattern=_daily(12), one_time_at=None, timezone=_TZ, now=_NOW
    )
    assert preview.quiet_hours_offer is None
