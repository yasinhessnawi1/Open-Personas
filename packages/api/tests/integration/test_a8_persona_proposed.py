"""A8 T7 — persona-proposed reschedule: distinguishing provenance + the negative (bars 1, 2, 5).

The propose-first STRUCTURE is proven in T4 (test_a8_propose_first.py — propose writes nothing;
apply refuses anything not user-confirmed). This adds the T7 proofs: the applied audit provenance
DISTINGUISHES a persona-proposed-user-confirmed change from a user-initiated one, a declined /
ignored proposal makes ZERO schedule writes, and the CAS door stays the only mutation site.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.errors import OriginationForbiddenError
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule, resolve_proposal
from persona_api.schedules import ScheduleStore
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


def _daily(hour: int) -> RecurrenceRule:
    return RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(hour,), byminute=(0,))


@pytest.fixture
def store() -> Iterator[ScheduleStore]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield ScheduleStore(engine)
    engine.dispose()


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


def _reschedule_provenances(engine: Engine) -> list[str]:
    with engine.begin() as conn:
        return [
            str(r["metadata"].get("provenance", ""))
            for r in conn.execute(
                text(
                    "SELECT metadata FROM audit_log "
                    "WHERE target = 's1' AND action = 'schedule.reschedule'"
                )
            )
            .mappings()
            .all()
        ]


def _propose(store: ScheduleStore) -> object:
    return propose_reschedule(
        store,
        proposal_id="p-quiet",
        owner_id="user_a",
        schedule_id="s1",
        persona_id="persona_1",
        new_recurrence=_daily(9),
        new_one_time=None,
        new_timezone="Europe/Oslo",
        reason="the 08:00 run keeps hitting your quiet hours",
        now=_NOW,
    )


def test_persona_proposed_confirmed_provenance_is_distinguishable(
    store: ScheduleStore, migrated_engine: Engine
) -> None:
    """Bar 1: a persona-proposed-user-confirmed apply is auditably distinct from user-initiated."""
    _seed_user(migrated_engine)
    _create(store, hour=8)

    # (a) a user-INITIATED reschedule through the door.
    current = store.get("user_a", "s1")
    reschedule(
        store,
        migrated_engine,
        owner_id="user_a",
        schedule_id="s1",
        new_schedule=current.model_copy(update={"recurrence": _daily(10)}),
        actor=RescheduleActor.USER_VIA_UI,
        provenance="calendar edit",
        now=_NOW,
    )
    # (b) a persona-PROPOSED reschedule, user-confirmed, applied.
    confirmed = resolve_proposal(_propose(store), approve=True, resolved_by="user_a", now=_NOW)
    apply_proposal(store, migrated_engine, confirmed, now=_NOW)

    provs = _reschedule_provenances(migrated_engine)
    assert len(provs) == 2
    # Exactly one names the persona proposal (the distinguishing marker) + the resolver.
    persona_prov = [p for p in provs if "persona proposal" in p]
    assert len(persona_prov) == 1
    assert "p-quiet" in persona_prov[0]
    assert "user_a" in persona_prov[0]  # the confirmation reference
    # The other is the plain user-initiated one (no persona-proposal marker).
    assert any("persona proposal" not in p and "calendar edit" in p for p in provs)


def test_rejected_proposal_makes_zero_schedule_writes(
    store: ScheduleStore, migrated_engine: Engine
) -> None:
    """Bar 2 (the negative): a declined proposal applies nothing and audits nothing."""
    _seed_user(migrated_engine)
    _create(store, hour=8)
    rejected = resolve_proposal(_propose(store), approve=False, resolved_by="user_a", now=_NOW)
    with pytest.raises(OriginationForbiddenError):
        apply_proposal(store, migrated_engine, rejected, now=_NOW)
    assert store.get("user_a", "s1").recurrence.byhour == (8,)  # type: ignore[union-attr] — unchanged
    assert _reschedule_provenances(migrated_engine) == []  # zero writes


def test_ignored_proposal_makes_zero_schedule_writes(
    store: ScheduleStore, migrated_engine: Engine
) -> None:
    """Bar 2: a proposal never resolved (ignored) never applies — the PENDING record is inert."""
    _seed_user(migrated_engine)
    _create(store, hour=8)
    pending = _propose(store)  # never resolved
    with pytest.raises(OriginationForbiddenError):
        apply_proposal(store, migrated_engine, pending, now=_NOW)  # type: ignore[arg-type]
    assert store.get("user_a", "s1").recurrence.byhour == (8,)  # type: ignore[union-attr]
    assert _reschedule_provenances(migrated_engine) == []
