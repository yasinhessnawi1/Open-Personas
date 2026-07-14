"""R9-037 — the schedule-tombstone gate on the REAL stack (real Postgres, :5436).

The owner reported: schedules they EDIT or DELETE get silently re-created/re-scheduled
by the persona's autonomous machinery. Phase 1 (see
``docs/specs/phase3/spec_R9/evidence/R9-037-fix.md``) confirmed the actor from the
dev-DB audit trail: ``persona_api.initiative.handler.ensure_initiative_schedule`` (the
A5 per-persona scan-schedule ensure) is a bare ``NOT EXISTS`` check with zero history
awareness, called both by the leader-gated ``InitiativeProvisioner`` sweep (hourly, AND
immediately on every worker restart — ``_last_initiative_provision`` resets to ``None``
on process boot) and by the initiative dial chat-verb
(``InitiativeVerbService._apply_dial``).

These tests drive the REAL re-creation paths — the actual ``InitiativeProvisioner.
run_once`` and the actual ``InitiativeVerbService.apply`` — never a hand-forced
terminal step, per the house "no hand-invoked step" discipline (A5/A4's own tests).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.errors import OriginationForbiddenError, ScheduleNotFoundError
from persona.initiative import InitiativeSettings
from persona.schedules import RecurrenceKind, RecurrencePattern
from persona.tasks import TaskState
from persona_api.initiative.delivery import InitiativeDeliveryExecutor
from persona_api.initiative.handler import ensure_initiative_schedule, initiative_schedule_id
from persona_api.initiative.provisioner import InitiativeProvisioner
from persona_api.initiative.store import DeclineStore, InitiativeLedger
from persona_api.initiative.verb_service import InitiativeVerbService
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.schedules.reschedule import RescheduleActor, reschedule
from persona_api.schedules.store import ScheduleStore
from persona_api.schedules.tombstones import ScheduleTombstoneStore, TombstoneAction
from persona_api.services.calendar_reschedule_service import apply_calendar_reschedule
from persona_api.services.schedule_create_service import create_user_schedule
from persona_api.services.schedule_delete_service import delete_schedule_with_intent
from persona_api.tasks.store import TaskStore
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_user_with_persona(engine: Engine, owner: str, persona: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": owner, "e": f"{owner}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": persona, "u": owner},
        )


def _audit_rows(
    engine: Engine, *, target: str, action: str | None = None
) -> list[dict[str, object]]:
    with engine.begin() as conn:
        sql = "SELECT * FROM audit_log WHERE target = :t"
        params: dict[str, object] = {"t": target}
        if action is not None:
            sql += " AND action = :a"
            params["a"] = action
        return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def _notification_rows(engine: Engine, *, kind: str, ref_id: str) -> list[dict[str, object]]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT * FROM notifications WHERE kind = :k AND ref_id = :r"),
                {"k": kind, "r": ref_id},
            )
            .mappings()
            .all()
        ]


# --------------------------------------------------------------------------------
# ScheduleTombstoneStore — pure store behaviour
# --------------------------------------------------------------------------------


def test_tombstone_store_records_and_finds_by_schedule_id(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    owner, persona = "own_r9037_a", "pers_r9037_a"
    _seed_user_with_persona(migrated_engine, owner, persona)
    store = ScheduleTombstoneStore(app_engine)
    sid = initiative_schedule_id(persona)

    token = current_user_id.set(owner)
    try:
        record = store.record(
            owner,
            schedule_id=sid,
            persona_id=persona,
            target_job_type="initiative_scan",
            title_key=None,
            action=TombstoneAction.DELETED,
            reason={"cadence": "every day at 07:00 your time"},
            now=_NOW,
        )
        assert record.schedule_id == sid

        found = store.find_recent(owner, schedule_id=sid, window_days=30, now=_NOW)
        assert found is not None
        assert found.id == record.id

        # A DIFFERENT schedule id never matches.
        other = store.find_recent(owner, schedule_id="initsched:other", window_days=30, now=_NOW)
        assert other is None
        # window_days=0 is the explicit escape hatch — never matches, regardless of age.
        assert store.find_recent(owner, schedule_id=sid, window_days=0, now=_NOW) is None
    finally:
        current_user_id.reset(token)
    rows = _audit_rows(migrated_engine, target=sid, action="schedule.tombstone_recorded")
    assert len(rows) == 1


def test_tombstone_store_window_expiry(migrated_engine: Engine, app_engine: Engine) -> None:
    owner, persona = "own_r9037_b", "pers_r9037_b"
    _seed_user_with_persona(migrated_engine, owner, persona)
    store = ScheduleTombstoneStore(app_engine)
    sid = initiative_schedule_id(persona)

    token = current_user_id.set(owner)
    try:
        store.record(
            owner,
            schedule_id=sid,
            persona_id=persona,
            target_job_type="initiative_scan",
            title_key=None,
            action=TombstoneAction.DELETED,
            reason={},
            now=_NOW,
        )
        # Still inside the 30-day window.
        soon = _NOW + timedelta(days=10)
        assert store.find_recent(owner, schedule_id=sid, window_days=30, now=soon) is not None
        # Past the window — the cool-down elapsed; no longer matches.
        later = _NOW + timedelta(days=40)
        assert store.find_recent(owner, schedule_id=sid, window_days=30, now=later) is None
    finally:
        current_user_id.reset(token)


def test_tombstone_store_title_key_content_match(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """A seam whose id is freshly minted per attempt still catches a re-creation via
    the normalized (target_job_type, title_key) content-identity leg."""
    owner, persona = "own_r9037_c", "pers_r9037_c"
    _seed_user_with_persona(migrated_engine, owner, persona)
    store = ScheduleTombstoneStore(app_engine)

    token = current_user_id.set(owner)
    try:
        store.record(
            owner,
            schedule_id="sched-old-id-123",
            persona_id=persona,
            target_job_type="task_scheduled_fire",
            title_key="call mom",
            action=TombstoneAction.DELETED,
            reason={"subject": "Call mom"},
            now=_NOW,
        )
        # A brand-new (never-seen) id, same normalized content — still matches.
        found = store.find_recent(
            owner,
            schedule_id="sched-freshly-minted-456",
            target_job_type="task_scheduled_fire",
            title_key="call mom",
            window_days=30,
            now=_NOW,
        )
        assert found is not None
        assert found.schedule_id == "sched-old-id-123"
        # Different content — no match.
        assert (
            store.find_recent(
                owner,
                schedule_id="sched-freshly-minted-456",
                target_job_type="task_scheduled_fire",
                title_key="water the plants",
                window_days=30,
                now=_NOW,
            )
            is None
        )
    finally:
        current_user_id.reset(token)


# --------------------------------------------------------------------------------
# ensure_initiative_schedule — the direct unit-of-behaviour gate
# --------------------------------------------------------------------------------


def test_ensure_initiative_schedule_refuses_on_tombstone_match(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    owner, persona = "own_r9037_d", "pers_r9037_d"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    tombstones = ScheduleTombstoneStore(app_engine)
    sid = initiative_schedule_id(persona)
    settings = InitiativeSettings()

    token = current_user_id.set(owner)
    try:
        tombstones.record(
            owner,
            schedule_id=sid,
            persona_id=persona,
            target_job_type="initiative_scan",
            title_key=None,
            action=TombstoneAction.DELETED,
            reason={},
            now=_NOW,
        )
        result = ensure_initiative_schedule(
            schedules,
            owner_id=owner,
            persona_id=persona,
            timezone="Europe/Oslo",
            settings=settings,
            now=_NOW,
            tombstones=tombstones,
            tombstone_window_days=30,
            seam="test",
        )
        assert result.created is False
        assert result.refused is True
        assert result.schedule_id == sid
        with pytest.raises(ScheduleNotFoundError):
            schedules.get(owner, sid)
    finally:
        current_user_id.reset(token)
    refusals = _audit_rows(migrated_engine, target=sid, action="schedule.recreate_refused")
    assert len(refusals) == 1
    assert refusals[0]["metadata"]["seam"] == "test"


def test_ensure_initiative_schedule_creates_normally_without_tombstone(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Regression pin: the pre-R9-037 ensure behaviour is unchanged when there is
    nothing to refuse (``tombstones=None`` AND the wired-but-empty case)."""
    owner, persona = "own_r9037_e", "pers_r9037_e"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    settings = InitiativeSettings()

    token = current_user_id.set(owner)
    try:
        result = ensure_initiative_schedule(
            schedules,
            owner_id=owner,
            persona_id=persona,
            timezone="Europe/Oslo",
            settings=settings,
            now=_NOW,
        )
        assert result.created is True
        assert result.refused is False
        again = ensure_initiative_schedule(
            schedules,
            owner_id=owner,
            persona_id=persona,
            timezone="Europe/Oslo",
            settings=settings,
            now=_NOW,
            tombstones=ScheduleTombstoneStore(app_engine),
            tombstone_window_days=30,
        )
        assert again.created is False
        assert again.refused is False  # already exists — not a refusal
    finally:
        current_user_id.reset(token)


# --------------------------------------------------------------------------------
# The real re-creation chains: InitiativeProvisioner + the dial chat-verb
# --------------------------------------------------------------------------------


def test_provisioner_real_chain_refuses_recently_deleted_schedule(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """THE confirmed bug's exact repro + fix, end to end, through the real sweep."""
    owner, persona = "own_r9037_f", "pers_r9037_f"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    task_store = TaskStore(app_engine)
    tombstones = ScheduleTombstoneStore(app_engine)
    sid = initiative_schedule_id(persona)

    provisioner = InitiativeProvisioner(
        dispatch_engine=migrated_engine,
        store=schedules,
        settings=InitiativeSettings(),
        default_timezone="Europe/Oslo",
        tombstones=tombstones,
        tombstone_window_days=30,
    )
    # First sweep: the persona is pre-existing (propose-only default), no schedule yet.
    assert provisioner.run_once(now=_NOW) == 1
    token = current_user_id.set(owner)
    try:
        schedules.get(owner, sid)  # exists
    finally:
        current_user_id.reset(token)

    # The user deletes it via the real route service.
    token = current_user_id.set(owner)
    try:
        outcome = delete_schedule_with_intent(
            schedule_store=schedules,
            task_store=task_store,
            tombstones=tombstones,
            rls_engine=app_engine,
            owner_id=owner,
            schedule_id=sid,
            now=_NOW,
        )
        assert outcome.schedule_id == sid
        assert outcome.paused_task_id is None  # initiative_scan has no backing task
        with pytest.raises(ScheduleNotFoundError):
            schedules.get(owner, sid)
    finally:
        current_user_id.reset(token)

    # THE real re-creation path: the SAME sweep, run again — the confirmed actor.
    later = _NOW + timedelta(hours=1)
    assert provisioner.run_once(now=later) == 0  # refused, not silently re-created
    token = current_user_id.set(owner)
    try:
        with pytest.raises(ScheduleNotFoundError):
            schedules.get(owner, sid)
    finally:
        current_user_id.reset(token)
    refusals = _audit_rows(migrated_engine, target=sid, action="schedule.recreate_refused")
    assert len(refusals) == 1
    assert refusals[0]["metadata"]["seam"] == "initiative_provisioner"


@pytest.mark.asyncio
async def test_dial_verb_real_chain_also_refuses(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The SECOND identified re-creation seam — the same shared ``ensure`` function,
    reached through ``InitiativeVerbService._apply_dial`` (a chat dial-verb turn)."""
    owner, persona = "own_r9037_g", "pers_r9037_g"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    task_store = TaskStore(app_engine)
    tombstones = ScheduleTombstoneStore(app_engine)
    sid = initiative_schedule_id(persona)
    ledger = InitiativeLedger(app_engine)

    verb_service = InitiativeVerbService(
        rls_engine=app_engine,
        schedules=schedules,
        ledger=ledger,
        declines=DeclineStore(app_engine),
        executor=InitiativeDeliveryExecutor(
            ledger=ledger,
            tasks=task_store,
            schedules=schedules,
            timezone_for="Europe/Oslo",
            rls_engine=app_engine,
        ),
        settings=InitiativeSettings(),
        tombstones=tombstones,
        tombstone_window_days=30,
    )

    token = current_user_id.set(owner)
    try:
        # An initial dial-verb turn provisions the schedule (the existing A5-D-1 lazy path).
        await verb_service.apply(
            owner_id=owner, persona_id=persona, verb="dial_act", notice_id=None
        )
        schedules.get(owner, sid)

        delete_schedule_with_intent(
            schedule_store=schedules,
            task_store=task_store,
            tombstones=tombstones,
            rls_engine=app_engine,
            owner_id=owner,
            schedule_id=sid,
            now=_NOW,
        )
        with pytest.raises(ScheduleNotFoundError):
            schedules.get(owner, sid)

        # THE real re-creation path: a LATER dial-verb turn (e.g. re-affirming the dial) —
        # not a fresh ask for THIS schedule back — must not silently resurrect it.
        await verb_service.apply(
            owner_id=owner, persona_id=persona, verb="dial_propose_only", notice_id=None
        )
        with pytest.raises(ScheduleNotFoundError):
            schedules.get(owner, sid)
    finally:
        current_user_id.reset(token)
    refusals = _audit_rows(migrated_engine, target=sid, action="schedule.recreate_refused")
    assert len(refusals) == 1
    assert refusals[0]["metadata"]["seam"] == "initiative_dial_verb"


def test_tombstone_window_elapsed_allows_recreation(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The cool-down is not permanent (design: 30 days default, env-tunable) — once the
    window elapses, the population-level self-heal resumes (the dial stays the
    authoritative permanent off-switch)."""
    owner, persona = "own_r9037_h", "pers_r9037_h"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    tombstones = ScheduleTombstoneStore(app_engine)
    sid = initiative_schedule_id(persona)

    provisioner = InitiativeProvisioner(
        dispatch_engine=migrated_engine,
        store=schedules,
        settings=InitiativeSettings(),
        default_timezone="Europe/Oslo",
        tombstones=tombstones,
        tombstone_window_days=30,
    )
    token = current_user_id.set(owner)
    try:
        tombstones.record(
            owner,
            schedule_id=sid,
            persona_id=persona,
            target_job_type="initiative_scan",
            title_key=None,
            action=TombstoneAction.DELETED,
            reason={},
            now=_NOW,
        )
    finally:
        current_user_id.reset(token)
    just_inside = _NOW + timedelta(days=29)
    assert provisioner.run_once(now=just_inside) == 0  # still refused
    well_past = _NOW + timedelta(days=31)
    assert provisioner.run_once(now=well_past) == 1  # the window elapsed — resumes


def test_unrelated_persona_not_blocked_by_anothers_tombstone(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """False-positive pin: a tombstoned persona never blocks a DIFFERENT persona's
    genuinely-new schedule in the SAME sweep batch (exact-id matching, correctly scoped)."""
    owner = "own_r9037_i"
    tombstoned_persona, fresh_persona = "pers_r9037_i1", "pers_r9037_i2"
    _seed_user_with_persona(migrated_engine, owner, tombstoned_persona)
    _seed_user_with_persona(migrated_engine, owner, fresh_persona)
    schedules = ScheduleStore(app_engine)
    tombstones = ScheduleTombstoneStore(app_engine)
    tombstoned_sid = initiative_schedule_id(tombstoned_persona)
    fresh_sid = initiative_schedule_id(fresh_persona)

    token = current_user_id.set(owner)
    try:
        tombstones.record(
            owner,
            schedule_id=tombstoned_sid,
            persona_id=tombstoned_persona,
            target_job_type="initiative_scan",
            title_key=None,
            action=TombstoneAction.DELETED,
            reason={},
            now=_NOW,
        )
    finally:
        current_user_id.reset(token)

    provisioner = InitiativeProvisioner(
        dispatch_engine=migrated_engine,
        store=schedules,
        settings=InitiativeSettings(),
        default_timezone="Europe/Oslo",
        tombstones=tombstones,
        tombstone_window_days=30,
    )
    # Exactly ONE created (the fresh persona) — the tombstoned persona is refused,
    # never blocking its sibling.
    assert provisioner.run_once(now=_NOW) == 1
    token = current_user_id.set(owner)
    try:
        with pytest.raises(ScheduleNotFoundError):
            schedules.get(owner, tombstoned_sid)
        schedules.get(owner, fresh_sid)  # exists — unaffected
    finally:
        current_user_id.reset(token)


# --------------------------------------------------------------------------------
# The task-bridge semantic (design point 3)
# --------------------------------------------------------------------------------


def test_delete_schedule_pauses_linked_task_and_notifies(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    owner, persona = "own_r9037_j", "pers_r9037_j"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    tasks = TaskStore(app_engine)
    tombstones = ScheduleTombstoneStore(app_engine)

    token = current_user_id.set(owner)
    try:
        result = create_user_schedule(
            app_engine,
            schedules,
            tasks,
            owner_id=owner,
            pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=9, minute=0),
            one_time_at=None,
            timezone="Europe/Oslo",
            persona_id=persona,
            subject="call mom",
            idempotency_key="r9037-task-pause-1",
            now=_NOW,
        )
        task_before = tasks.get(owner, result.task_id)
        assert task_before.state is TaskState.WAITING
        assert task_before.paused is False

        outcome = delete_schedule_with_intent(
            schedule_store=schedules,
            task_store=tasks,
            tombstones=tombstones,
            rls_engine=app_engine,
            owner_id=owner,
            schedule_id=result.schedule_id,
            now=_NOW,
        )
        assert outcome.paused_task_id == result.task_id

        task_after = tasks.get(owner, result.task_id)
        assert task_after.paused is True
        # The task itself is not silently forgotten — still WAITING, just paused
        # (it cannot silently re-arm: its schedule is gone and nothing recreates it).
        assert task_after.state is TaskState.WAITING
    finally:
        current_user_id.reset(token)

    notif_rows = _notification_rows(
        migrated_engine, kind="schedule_deleted_task_paused", ref_id=result.task_id
    )
    assert len(notif_rows) == 1
    pause_audits = _audit_rows(migrated_engine, target=result.task_id, action="task.pause")
    assert len(pause_audits) == 1


def test_delete_schedule_terminal_task_is_benign_noop(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    owner, persona = "own_r9037_k", "pers_r9037_k"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    tasks = TaskStore(app_engine)
    tombstones = ScheduleTombstoneStore(app_engine)

    token = current_user_id.set(owner)
    try:
        result = create_user_schedule(
            app_engine,
            schedules,
            tasks,
            owner_id=owner,
            pattern=None,
            one_time_at=_NOW + timedelta(hours=1),
            timezone="Europe/Oslo",
            persona_id=persona,
            subject="one-off thing",
            idempotency_key="r9037-task-pause-2",
            now=_NOW,
        )
        # A schedule-backed task is born WAITING (not DEFINED) — resume, not start.
        tasks.resume(owner, result.task_id, now=_NOW)
        tasks.complete(owner, result.task_id, now=_NOW)

        outcome = delete_schedule_with_intent(
            schedule_store=schedules,
            task_store=tasks,
            tombstones=tombstones,
            rls_engine=app_engine,
            owner_id=owner,
            schedule_id=result.schedule_id,
            now=_NOW,
        )
        assert outcome.paused_task_id is None  # terminal — TaskStateError caught, benign

        task_after = tasks.get(owner, result.task_id)
        assert task_after.state is TaskState.COMPLETED
        assert task_after.paused is False
    finally:
        current_user_id.reset(token)
    assert (
        _notification_rows(
            migrated_engine, kind="schedule_deleted_task_paused", ref_id=result.task_id
        )
        == []
    )


# --------------------------------------------------------------------------------
# Edit writes a tombstone too (design point 1) + the user-create door is never gated
# --------------------------------------------------------------------------------


def test_edit_schedule_writes_an_edited_tombstone(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    owner, persona = "own_r9037_l", "pers_r9037_l"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    tasks = TaskStore(app_engine)

    token = current_user_id.set(owner)
    try:
        result = create_user_schedule(
            app_engine,
            schedules,
            tasks,
            owner_id=owner,
            pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=9, minute=0),
            one_time_at=None,
            timezone="Europe/Oslo",
            persona_id=persona,
            subject="stretch",
            idempotency_key="r9037-edit-1",
            now=_NOW,
        )
        apply_calendar_reschedule(
            schedules,
            app_engine,
            owner_id=owner,
            schedule_id=result.schedule_id,
            pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=18, minute=0),
            one_time_at=None,
            timezone="Europe/Oslo",
            now=_NOW,
        )
        found = ScheduleTombstoneStore(app_engine).find_recent(
            owner,
            schedule_id=result.schedule_id,
            window_days=30,
            now=_NOW,
            action=TombstoneAction.EDITED,
        )
        assert found is not None
        assert found.action is TombstoneAction.EDITED
        assert "old_cadence" in found.reason
    finally:
        current_user_id.reset(token)


def test_persona_proposed_reschedule_still_refused_structurally(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Design point 4 (prove, don't duplicate): the ONLY way a cadence ever changes is
    through a user-attributed actor — a persona-proposed DIRECT write remains refused
    by the pre-existing A8-D-12 structural gate. No autonomous "revert to a remembered
    cadence" path exists to begin with."""
    owner, persona = "own_r9037_m", "pers_r9037_m"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    tasks = TaskStore(app_engine)

    token = current_user_id.set(owner)
    try:
        result = create_user_schedule(
            app_engine,
            schedules,
            tasks,
            owner_id=owner,
            pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=9, minute=0),
            one_time_at=None,
            timezone="Europe/Oslo",
            persona_id=persona,
            subject="water plants",
            idempotency_key="r9037-persona-proposed-1",
            now=_NOW,
        )
        current = schedules.get(owner, result.schedule_id)
        with pytest.raises(OriginationForbiddenError, match="persona-proposed"):
            reschedule(
                schedules,
                app_engine,
                owner_id=owner,
                schedule_id=result.schedule_id,
                new_schedule=current,
                actor=RescheduleActor.PERSONA_PROPOSED,
                provenance="test",
                now=_NOW,
            )
    finally:
        current_user_id.reset(token)


def test_user_create_door_never_gated_by_a_tombstone(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """False-positive pin on the user-facing door: deleting a reminder and then
    EXPLICITLY re-creating "the same" one via the direct create door (never an
    autonomous seam) always succeeds — R9-037 gates autonomous origination only."""
    owner, persona = "own_r9037_n", "pers_r9037_n"
    _seed_user_with_persona(migrated_engine, owner, persona)
    schedules = ScheduleStore(app_engine)
    tasks = TaskStore(app_engine)
    tombstones = ScheduleTombstoneStore(app_engine)

    token = current_user_id.set(owner)
    try:
        first = create_user_schedule(
            app_engine,
            schedules,
            tasks,
            owner_id=owner,
            pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=9, minute=0),
            one_time_at=None,
            timezone="Europe/Oslo",
            persona_id=persona,
            subject="call mom",
            idempotency_key="r9037-user-recreate-1",
            now=_NOW,
        )
        delete_schedule_with_intent(
            schedule_store=schedules,
            task_store=tasks,
            tombstones=tombstones,
            rls_engine=app_engine,
            owner_id=owner,
            schedule_id=first.schedule_id,
            now=_NOW,
        )
        # A tombstone with the matching content identity now exists...
        assert (
            tombstones.find_recent(
                owner,
                target_job_type="task_scheduled_fire",
                title_key="call mom",
                window_days=30,
                now=_NOW,
            )
            is not None
        )
        # ...but the user's OWN direct re-create (a DIFFERENT idempotency key — a
        # deliberate second ask) is never gated by it.
        second = create_user_schedule(
            app_engine,
            schedules,
            tasks,
            owner_id=owner,
            pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=9, minute=0),
            one_time_at=None,
            timezone="Europe/Oslo",
            persona_id=persona,
            subject="call mom",
            idempotency_key="r9037-user-recreate-2",
            now=_NOW,
        )
        assert second.schedule_id != first.schedule_id
        schedules.get(owner, second.schedule_id)  # exists — never refused
    finally:
        current_user_id.reset(token)
