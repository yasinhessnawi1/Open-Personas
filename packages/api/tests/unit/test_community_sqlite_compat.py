"""Community-SQLite compatibility guards (the A8 render-evidence findings, 2026-07-04).

The community edition serves the SAME service/store code over the D-33 SQLite
engine. Three Postgres-isms broke every A-track surface there — each is pinned
here on a real in-memory SQLite engine built from the community metadata, so
the class cannot silently return:

1. ``set_current_user`` must no-op on the sqlite dialect (``set_config`` does
   not exist there; community scoping is by owner predicate — D-33-X).
2. ``ScheduleStore`` must round-trip its frozen tz-aware models over SQLite's
   tz-less DateTime storage (the ``_aware_utc`` row-boundary coercion).
3. The occurrences read model (incl. the ``_fire_history`` expanding-IN query
   and its JSON-text metadata decode) must execute on SQLite end-to-end.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule
from persona_api.config import APIConfig
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.engine import rls_connection
from persona_api.schedules.store import ScheduleStore
from persona_api.services.occurrences_service import list_occurrences
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "sqlite-compat-owner"
_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_community_engine(tmp_path / "compat.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="compat@localhost")
    return eng


def test_rls_connection_noops_the_guc_on_sqlite(engine: Engine) -> None:
    """Fix 1: every rls_connection caller works on the community engine."""
    with rls_connection(engine, _OWNER) as conn:
        assert conn.execute(text("SELECT 1")).scalar_one() == 1


def test_schedule_store_round_trips_aware_datetimes_on_sqlite(engine: Engine) -> None:
    """Fix 2: SQLite drops tzinfo; the row boundary re-attaches UTC losslessly."""
    store = ScheduleStore(engine)
    created = store.create(
        Schedule(
            id="compat-daily",
            owner_id=_OWNER,
            timezone="Europe/Oslo",
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(8,), byminute=(0,)),
            target_job_type="briefing",
            created_at=_NOW,
            updated_at=_NOW,
        ),
        now=_NOW,
    )
    fetched = store.get(_OWNER, "compat-daily")
    assert fetched.next_fire_at is not None
    assert fetched.next_fire_at.tzinfo is not None  # aware, never naive
    assert fetched.next_fire_at == created.next_fire_at
    assert fetched.created_at == _NOW


def test_occurrences_read_model_runs_on_sqlite_incl_fire_history(engine: Engine) -> None:
    """Fix 3: the windowed read model (expanding-IN history + JSON-text decode)."""
    store = ScheduleStore(engine)
    store.create(
        Schedule(
            id="compat-hist",
            owner_id=_OWNER,
            timezone="Europe/Oslo",
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(9,), byminute=(0,)),
            target_job_type="briefing",
            created_at=_NOW,
            updated_at=_NOW,
        ),
        now=_NOW,
    )
    fire_time = _NOW + timedelta(hours=1)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (id, user_id, action, target, metadata, created_at) "
                "VALUES (:id, :u, 'schedule.fire', 'compat-hist', :m, :c)"
            ),
            {
                "id": "compat-audit-1",
                "u": _OWNER,
                # Raw TEXT on SQLite — exactly the shape _fire_history must decode.
                "m": json.dumps({"fire_time": fire_time.isoformat()}),
                "c": fire_time.isoformat(),
            },
        )
    result = list_occurrences(
        engine,
        owner_id=_OWNER,
        from_=_NOW,
        to=_NOW + timedelta(days=3),
        config=APIConfig(),
    )
    assert len(result.occurrences) >= 1
    assert [h.status for h in result.history] == ["ran"]
    assert result.history[0].at == fire_time


def test_telemetry_flush_lands_rows_on_sqlite(engine: Engine) -> None:
    """Fix 4 (R4-C1-4): the telemetry flush must mint its PK client-side.

    The canonical column's ``gen_random_uuid()`` server default is
    Postgres-only; relying on it made EVERY community flush die on NOT NULL
    and telemetry silently dropped 100% of rows. The flush now mints the id.
    """
    import asyncio
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from persona_api.middleware.request_telemetry import TelemetryBuffer, TelemetryEvent

    buf = TelemetryBuffer(engine)
    buf.record(
        TelemetryEvent(
            timestamp=_dt(2026, 7, 4, 12, 0, tzinfo=_UTC),
            method="GET",
            route_template="/v1/personas/{persona_id}",
            status_code=200,
            duration_ms=12.5,
        )
    )
    asyncio.run(buf._flush_once())  # noqa: SLF001 — the flush IS the unit under test
    with engine.begin() as conn:
        n = conn.execute(text("SELECT count(*) FROM request_telemetry")).scalar_one()
        row_id = conn.execute(text("SELECT id FROM request_telemetry")).scalar_one()
    assert n == 1
    assert row_id  # non-null, client-minted
