"""A7 T5 — storm safety on the REAL path: burst coalescing + R7 drop-with-surface (criterion 5).

Two load-bearing proofs, both a genuine concurrent burst through the REAL dispatcher
(:func:`~persona_api.events.build_event_dispatcher` — real ``claim_fire``, real ``book_day_spend``
day-cap, real ``audit_log``, real P6 bell):

1. an N-event burst inside the cooldown window coalesces to EXACTLY ONE fire, and the pending count
   accumulated on the trigger row is accurate — the next window's fire reports it ("and N more");
2. R7's day-cap is consulted before the fire; over-cap ⇒ the fire is DROPPED, an
   ``event_trigger.storm_dropped`` audit row lands, a durable ``event_trigger_dropped`` P6 bell is
   written, and NO leg is enqueued — never silent, never unbounded.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.credits.service import book_day_spend
from persona.events import ConnectorMessageReceived, EventKind, FireTaskLeg, MessageFilter
from persona_api.config import APIConfig
from persona_api.events import (
    DispatchDisposition,
    EventTriggerRecord,
    EventTriggerStore,
    build_event_dispatcher,
)
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "own_a7_storm5"
_PERSONA = "pers_a7_storm5"
_TASK = "task_a7_storm5"
_TRIGGER = "trg_a7_storm5"
_NOW = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — migrations first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": _PERSONA, "u": _OWNER},
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, persona_id, contract_json) "
                'VALUES (:t, :u, :p, \'{"goal": "watch the inbox"}\') ON CONFLICT DO NOTHING'
            ),
            {"t": _TASK, "u": _OWNER, "p": _PERSONA},
        )


def _make_trigger(store: EventTriggerStore) -> None:
    store.create_if_absent(
        EventTriggerRecord(
            id=_TRIGGER,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            task_id=_TASK,
            event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
            platform="email",
            filter=MessageFilter(platform="email"),
            action=FireTaskLeg(task_id=_TASK),
            enabled=True,
            disabled_reason=None,
            last_fired_at=None,
            pending_coalesced_count=0,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        now=_NOW,
    )


def _event(event_id: str) -> ConnectorMessageReceived:
    return ConnectorMessageReceived(
        event_id=event_id,
        owner_id=_OWNER,
        occurred_at=_NOW,
        platform="email",
        sender_id="landlord@example.com",
        body="rent",
        conversation_id="conv-1",
        persona_id=_PERSONA,
        message_id=event_id,
    )


def _burst(dispatcher: object, fanout: int, *, now: datetime) -> list[DispatchDisposition]:
    """Release ``fanout`` threads simultaneously; return every dispatch disposition."""
    barrier = threading.Barrier(fanout)

    def _arrive(i: int) -> list[DispatchDisposition]:
        current_user_id.set(_OWNER)  # per-thread RLS scope for book_day_spend's own connection
        barrier.wait()
        return [
            o.disposition for o in dispatcher.dispatch(_event(f"evt-{now.minute}-{i}"), now=now)
        ]  # type: ignore[attr-defined]

    with ThreadPoolExecutor(max_workers=fanout) as pool:
        return [d for fut in pool.map(_arrive, range(fanout)) for d in fut]


def _count_jobs(su: Engine, job_type: str) -> int:
    with su.begin() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM jobs WHERE owner_id = :o AND type = :t"),
                {"o": _OWNER, "t": job_type},
            ).scalar_one()
        )


def _audit_actions(su: Engine) -> list[str]:
    with su.begin() as conn:
        return [
            r[0]
            for r in conn.execute(
                text("SELECT action FROM audit_log WHERE user_id = :o"), {"o": _OWNER}
            ).all()
        ]


def _notification_kinds(su: Engine) -> list[str]:
    with su.begin() as conn:
        return [
            r[0]
            for r in conn.execute(
                text("SELECT kind FROM notifications WHERE owner_id = :o"), {"o": _OWNER}
            ).all()
        ]


def test_real_burst_coalesces_to_one_fire_with_accurate_count(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    _make_trigger(store)
    # Unlimited budget — this test isolates coalescing (R7 is the next test).
    dispatcher = build_event_dispatcher(
        rls_engine=app_engine, config=APIConfig(credits_max_per_day=0)
    )

    fanout = 10
    results = _burst(dispatcher, fanout, now=_NOW)
    assert results.count(DispatchDisposition.FIRED) == 1  # exactly one won the window
    assert results.count(DispatchDisposition.COALESCED) == fanout - 1  # the rest coalesced

    # The pending count on the trigger row is ACCURATE — every non-winner incremented it once.
    row = store.get(_OWNER, _TRIGGER)
    assert row is not None
    assert row.pending_coalesced_count == fanout - 1

    # The NEXT window's fire reports the coalesced burst ("and N more") and resets the count.
    later = _NOW + timedelta(seconds=400)
    outcome = dispatcher.dispatch(_event("evt-late"), now=later)
    assert outcome[0].disposition is DispatchDisposition.FIRED
    assert outcome[0].coalesced_count == fanout - 1
    row2 = store.get(_OWNER, _TRIGGER)
    assert row2 is not None
    assert row2.pending_coalesced_count == 0


def test_over_cap_burst_drops_with_audit_and_p6_surface(
    app_engine: Engine, migrated_engine: Engine, database_url: str
) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    _make_trigger(store)
    cap = 5
    # Pre-fill the day's spend to the cap so the next fire is over-cap (R7-D-3 conditional write).
    current_user_id.set(_OWNER)
    assert book_day_spend(rls_engine=app_engine, user_id=_OWNER, cost=cap, cap=cap) is True

    dispatcher = build_event_dispatcher(
        rls_engine=app_engine, config=APIConfig(credits_max_per_day=cap)
    )
    results = _burst(dispatcher, 6, now=_NOW)
    assert results.count(DispatchDisposition.FIRED) == 0  # nothing fired over-cap
    assert results.count(DispatchDisposition.DROPPED_OVER_CAP) == 1  # the window winner was dropped
    assert results.count(DispatchDisposition.COALESCED) == 5

    su = create_engine(database_url.replace("+asyncpg", "+psycopg"))
    try:
        assert "event_trigger.storm_dropped" in _audit_actions(su)  # audited, not silent
        assert "event_trigger_dropped" in _notification_kinds(su)  # surfaced to P6, durable
        assert _count_jobs(su, "task_leg") == 0  # never enqueued over-cap
    finally:
        su.dispose()
