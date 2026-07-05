"""A10 T1 — the user's create door lands real rows through A8's store (bars 1–5).

Real Postgres. ``create_user_schedule`` is what ``POST /v1/me/schedule`` calls. Proves:
the create lands a WAITING task + a fire-bridge schedule via ``ScheduleStore.create``
(bar 1); the service-level 422 causes — unknown/cross-tenant executor persona,
never-firing cadence, empty subject (bar 2); idempotent double-submit converges on ONE
pair while two deliberate submits create two (bar 3, both directions); the append-only
``schedule.create`` audit carries ``actor=user_via_ui`` + ``originator=user`` (bar 4);
and RLS is non-vacuous — both tenants create, neither reads the other's (bar 5).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from persona.errors import PersonaNotFoundError, ScheduleNeverFiresError, TaskNotFoundError
from persona.schedules import (
    RecurrenceKind,
    RecurrencePattern,
    next_fire_after,
)
from persona.tasks import TaskState, WaitKind
from persona_api.schedules import ScheduleStore
from persona_api.services.schedule_create_service import ScheduleCreateResult, create_user_schedule
from persona_api.tasks.scheduled_fire import TASK_SCHEDULED_FIRE_JOB_TYPE
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(migrated_engine: Engine) -> Iterator[Engine]:
    """The ``persona_app`` RLS engine the service runs on (seeding rides the superuser)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    eng = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine: Engine) -> ScheduleStore:
    return ScheduleStore(engine)


@pytest.fixture
def tasks(engine: Engine) -> TaskStore:
    return TaskStore(engine)


def _seed_user_with_persona(seed: Engine, uid: str, persona_id: str) -> None:
    with seed.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": uid, "e": f"{uid}@example.com"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
            {"p": persona_id, "o": uid},
        )


def _daily(hour: int = 9) -> RecurrencePattern:
    return RecurrencePattern(kind=RecurrenceKind.DAILY, hour=hour, minute=0)


def _create(
    engine: Engine,
    store: ScheduleStore,
    tasks: TaskStore,
    *,
    owner: str = "user_a",
    persona: str = "pa",
    key: str = "dialog-0001",
    pattern: RecurrencePattern | None = None,
    one_time_at: datetime | None = None,
    subject: str = "hydration",
) -> ScheduleCreateResult:
    if pattern is None and one_time_at is None:
        pattern = _daily()
    return create_user_schedule(
        engine,
        store,
        tasks,
        owner_id=owner,
        pattern=pattern,
        one_time_at=one_time_at,
        timezone="Europe/Oslo",
        persona_id=persona,
        subject=subject,
        idempotency_key=key,
        now=_NOW,
    )


def _audit_rows(seed: Engine, schedule_id: str) -> list[dict[str, object]]:
    with seed.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT metadata FROM audit_log "
                    "WHERE target = :t AND action = 'schedule.create'"
                ),
                {"t": schedule_id},
            )
            .mappings()
            .all()
        ]


# --- bar 1: the create lands the pair through A8's store --------------------------------


def test_create_lands_waiting_task_and_fire_bridge_schedule(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    result = _create(engine, store, tasks)
    assert result.created is True

    task = tasks.get("user_a", result.task_id)
    assert task.state is TaskState.WAITING  # dormant-but-runnable, never inert
    assert task.wait_kind is WaitKind.UNTIL_TIME
    assert task.persona_id == "pa"
    assert task.schedule_id == result.schedule_id
    assert "hydration" in task.contract.goal

    schedule = store.get("user_a", result.schedule_id)
    assert schedule.target_job_type == TASK_SCHEDULED_FIRE_JOB_TYPE
    assert schedule.payload_template == {"task_id": result.task_id}
    # The stored next fire is the ENGINE's, and the response echoed it.
    assert schedule.next_fire_at is not None
    assert schedule.next_fire_at == next_fire_after(schedule, after=schedule.created_at)
    assert result.next_fire is not None
    assert "every day at 09:00" in result.human_terms
    assert "FREQ=" not in result.human_terms  # no raw RRULE at any boundary


# --- bar 2: the service-level 422/404 causes --------------------------------------------


def test_unknown_executor_persona_is_refused_before_any_write(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    with pytest.raises(PersonaNotFoundError):
        _create(engine, store, tasks, persona="ghost")
    assert store.list_for_owner("user_a") == []


def test_cross_tenant_executor_persona_reads_as_absent(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    """RLS: B's persona is invisible to A — no existence oracle, no cross-tenant executor."""
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    _seed_user_with_persona(migrated_engine, "user_b", "pb")
    with pytest.raises(PersonaNotFoundError):
        _create(engine, store, tasks, owner="user_a", persona="pb")


def test_past_one_time_never_fires_and_writes_nothing(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    with pytest.raises(ScheduleNeverFiresError):
        _create(engine, store, tasks, one_time_at=_NOW - timedelta(days=1))
    assert store.list_for_owner("user_a") == []


def test_exhausted_until_bound_never_fires(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    stale = RecurrencePattern(
        kind=RecurrenceKind.DAILY, hour=9, minute=0, until=_NOW - timedelta(days=2)
    )
    with pytest.raises(ScheduleNeverFiresError):
        _create(engine, store, tasks, pattern=stale)


def test_empty_subject_after_normalisation_is_refused(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    with pytest.raises(ValueError, match="subject"):
        _create(engine, store, tasks, subject=" \n\t ")


# --- bar 3: idempotency, both directions -------------------------------------------------


def test_double_submit_same_key_converges_on_one_pair(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    """The double-click/retry: same dialog ⇒ same key ⇒ ONE task + ONE schedule."""
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    first = _create(engine, store, tasks, key="dialog-0001")
    second = _create(engine, store, tasks, key="dialog-0001")
    assert first.created is True
    assert second.created is False
    assert (second.task_id, second.schedule_id) == (first.task_id, first.schedule_id)
    assert len(store.list_for_owner("user_a")) == 1


def test_two_deliberate_submits_create_two(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    """The A10-D-6 inverse of A4-D-X: same content, two dialog-opens ⇒ two tasks."""
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    first = _create(engine, store, tasks, key="dialog-0001")
    second = _create(engine, store, tasks, key="dialog-0002")
    assert second.created is True
    assert second.task_id != first.task_id
    assert len(store.list_for_owner("user_a")) == 2


# --- bar 4: the append-only audit --------------------------------------------------------


def test_create_audit_carries_the_ui_actor_and_user_originator(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    result = _create(engine, store, tasks)
    rows = _audit_rows(migrated_engine, result.schedule_id)
    assert len(rows) == 1  # exactly one audit row per mutation
    meta = rows[0]["metadata"]
    assert meta["actor"] == "user_via_ui"
    assert meta["originator"] == "user"
    assert meta["subject"] == "hydration"


# --- bar 5: RLS non-vacuous ---------------------------------------------------------------


def test_rls_non_vacuous_both_tenants_create_neither_sees_the_other(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, migrated_engine: Engine
) -> None:
    _seed_user_with_persona(migrated_engine, "user_a", "pa")
    _seed_user_with_persona(migrated_engine, "user_b", "pb")
    a = _create(engine, store, tasks, owner="user_a", persona="pa", key="dialog-000a")
    b = _create(engine, store, tasks, owner="user_b", persona="pb", key="dialog-000b")

    a_schedules = {s.id for s in store.list_for_owner("user_a")}
    b_schedules = {s.id for s in store.list_for_owner("user_b")}
    assert a_schedules == {a.schedule_id}
    assert b_schedules == {b.schedule_id}

    with pytest.raises(TaskNotFoundError):
        tasks.get("user_a", b.task_id)  # B's task is invisible to A — no oracle
