"""Task continuation — disposition→state, waiting/resume, dormancy (Spec A2, T8).

Runs against real Postgres. Proves the four holds: the disposition→state-machine wiring
(continue / complete / fail), the ``waiting(until_time)`` self-continuation (idempotent,
rides A0 ``scheduled_at``), zero-cost dormancy (a waiting task = a state row, no running leg),
and the ``waiting(on_user)`` wait + the ``UserReply`` resume seam (``EventTrigger`` reserved).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from persona.errors import TaskLegFailedError
from persona.tasks import Contract, Task, TaskState, UserReply, WaitKind
from persona_api.jobs.queue import JobQueue
from persona_api.schedules.store import ScheduleStore
from persona_api.tasks import TaskContinuation, TaskStore
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.legs import LegDisposition, LegOutcome
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from persona.tasks import TaskCheckpoint  # isort: skip — grouped with task entities

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(hours=4)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_active_task(engine: Engine) -> Task:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('user_a','a@example.com')"))
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES ('persona_a','user_a','name: x')"
            )
        )
    tasks = TaskStore(engine)
    tasks.create(
        Task(
            id="t1",
            owner_id="user_a",
            persona_id="persona_a",
            contract=Contract(goal="find the cheapest fare"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    return tasks.start("user_a", "t1", now=_NOW)


def _outcome(
    task: Task, disposition: LegDisposition, *, resume_at: datetime | None = None
) -> LegOutcome:
    """A LegOutcome carrying a task with head=0 (post-append) — the continuation only reads
    task.id / task.head_checkpoint_seq / disposition / resume_at."""
    advanced = task.advance_checkpoint(0, now=_NOW)
    return LegOutcome(
        task=advanced,
        checkpoint=TaskCheckpoint(
            task_id="t1", leg_id="t1:leg:0", checkpoint_seq=0, next_step="x", updated_at=_NOW
        ),
        run=Run(
            persona_id="persona_a",
            task="x",
            status=RunStatus.COMPLETED,
            steps=[],
            started_at=_NOW,
            finished_at=_NOW,
        ),
        disposition=disposition,
        box_limit=None,
        spend={},
        resume_at=resume_at,
    )


def _leg_jobs(engine: Engine) -> list[dict[str, object]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT state, scheduled_at, payload FROM jobs "
                "WHERE type = 'task_leg' AND owner_id = 'user_a'"
            )
        ).all()
    return [{"state": r.state, "scheduled_at": r.scheduled_at, "payload": r.payload} for r in rows]


# --- disposition → state machine ---------------------------------------------


def test_continue_immediate_enqueues_next_leg(migrated_engine: Engine, app_engine: Engine) -> None:
    task = _seed_active_task(migrated_engine)
    cont = TaskContinuation(task_store=TaskStore(app_engine), queue=JobQueue(app_engine))
    cont.apply("user_a", _outcome(task, LegDisposition.CONTINUE), now=_NOW)
    jobs = _leg_jobs(migrated_engine)
    assert len(jobs) == 1
    assert jobs[0]["payload"]["predecessor_seq"] == 0  # the new head
    assert TaskStore(app_engine).get("user_a", "t1").state == TaskState.ACTIVE  # stays active


def test_continue_is_idempotent(migrated_engine: Engine, app_engine: Engine) -> None:
    task = _seed_active_task(migrated_engine)
    cont = TaskContinuation(task_store=TaskStore(app_engine), queue=JobQueue(app_engine))
    cont.apply("user_a", _outcome(task, LegDisposition.CONTINUE), now=_NOW)
    cont.apply("user_a", _outcome(task, LegDisposition.CONTINUE), now=_NOW)  # double-enqueue
    assert len(_leg_jobs(migrated_engine)) == 1  # A2-R-4 key dedups


def test_completed_completes_task_with_no_next_job(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    task = _seed_active_task(migrated_engine)
    cont = TaskContinuation(task_store=TaskStore(app_engine), queue=JobQueue(app_engine))
    cont.apply("user_a", _outcome(task, LegDisposition.COMPLETED), now=_NOW)
    assert TaskStore(app_engine).get("user_a", "t1").state == TaskState.COMPLETED
    assert len(_leg_jobs(migrated_engine)) == 0


# --- Spec A4 recurrence: occurrence-complete → WAITING (recurring) vs terminal (one-time) ------


def _attach_schedule(engine: Engine, *, recurring: bool, exhausted: bool = False) -> str:
    """Create a schedule + point ``t1`` at it; return the schedule id.

    ``recurring`` → a daily rule (a future occurrence exists → the task should survive a complete).
    ``exhausted`` → a recurring rule with COUNT=1 already past (no future fire → terminal, the leak
    the design must not create). Otherwise a one-time schedule already in the past → terminal.
    """
    from persona.schedules import RecurrenceRule, Schedule

    anchor = _NOW - timedelta(days=1)
    if recurring and not exhausted:
        sched = Schedule(
            id="s1",
            owner_id="user_a",
            timezone="Europe/Oslo",
            recurrence=RecurrenceRule.from_rrule_string("FREQ=DAILY;BYHOUR=6;BYMINUTE=0"),
            target_job_type="task_leg",
            payload_template={"task_id": "t1"},
            created_at=anchor,
            updated_at=anchor,
        )
    elif exhausted:
        sched = Schedule(
            id="s1",
            owner_id="user_a",
            timezone="Europe/Oslo",
            recurrence=RecurrenceRule.from_rrule_string("FREQ=DAILY;BYHOUR=6;BYMINUTE=0;COUNT=1"),
            target_job_type="task_leg",
            payload_template={"task_id": "t1"},
            created_at=anchor,
            updated_at=anchor,
        )
    else:
        sched = Schedule(
            id="s1",
            owner_id="user_a",
            timezone="Europe/Oslo",
            one_time_at=_NOW - timedelta(minutes=5),  # already fired → no future occurrence
            target_job_type="task_leg",
            payload_template={"task_id": "t1"},
            created_at=anchor,
            updated_at=anchor,
        )
    ScheduleStore(engine).create(sched, now=anchor)
    with engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET schedule_id = 's1' WHERE id = 't1'"))
    return "s1"


def _scheduled_task(
    engine: Engine, app_engine: Engine, *, recurring: bool, exhausted: bool = False
) -> Task:
    _seed_active_task(engine)
    _attach_schedule(engine, recurring=recurring, exhausted=exhausted)
    return TaskStore(app_engine).get("user_a", "t1")  # re-read with schedule_id set


def test_recurring_task_occurrence_complete_returns_to_waiting_not_terminal(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    # The load-bearing recurrence semantic: a completed leg on a recurring schedule is one
    # occurrence done, NOT the task — the task returns to WAITING(until_time) so the next
    # scheduled fire resumes it. Without this it would terminate after occurrence one.
    task = _scheduled_task(migrated_engine, app_engine, recurring=True)
    cont = TaskContinuation(
        task_store=TaskStore(app_engine),
        queue=JobQueue(app_engine),
        schedule_store=ScheduleStore(app_engine),
    )
    cont.apply("user_a", _outcome(task, LegDisposition.COMPLETED), now=_NOW)
    got = TaskStore(app_engine).get("user_a", "t1")
    assert got.state == TaskState.WAITING  # survives for the next fire
    assert got.wait_kind == WaitKind.UNTIL_TIME
    assert len(_leg_jobs(migrated_engine)) == 0  # the SCHEDULE re-arms the next leg, not us


def test_one_time_task_completes_terminally(migrated_engine: Engine, app_engine: Engine) -> None:
    task = _scheduled_task(migrated_engine, app_engine, recurring=False)
    cont = TaskContinuation(
        task_store=TaskStore(app_engine),
        queue=JobQueue(app_engine),
        schedule_store=ScheduleStore(app_engine),
    )
    cont.apply("user_a", _outcome(task, LegDisposition.COMPLETED), now=_NOW)
    assert TaskStore(app_engine).get("user_a", "t1").state == TaskState.COMPLETED  # runs once


def test_exhausted_recurring_rule_completes_terminally_no_leak(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    # A finite RRULE whose last occurrence already fired has no future fire → the task must
    # TERMINATE, not wait forever (the leak the design explicitly avoids).
    task = _scheduled_task(migrated_engine, app_engine, recurring=True, exhausted=True)
    cont = TaskContinuation(
        task_store=TaskStore(app_engine),
        queue=JobQueue(app_engine),
        schedule_store=ScheduleStore(app_engine),
    )
    cont.apply("user_a", _outcome(task, LegDisposition.COMPLETED), now=_NOW)
    assert TaskStore(app_engine).get("user_a", "t1").state == TaskState.COMPLETED


def test_failed_raises_for_retry(migrated_engine: Engine, app_engine: Engine) -> None:
    task = _seed_active_task(migrated_engine)
    cont = TaskContinuation(task_store=TaskStore(app_engine), queue=JobQueue(app_engine))
    with pytest.raises(TaskLegFailedError):
        cont.apply("user_a", _outcome(task, LegDisposition.FAILED), now=_NOW)
    assert TaskStore(app_engine).get("user_a", "t1").state == TaskState.ACTIVE  # unchanged
    assert len(_leg_jobs(migrated_engine)) == 0  # no continuation


# --- waiting(until_time): self-continuation + dormancy -----------------------


def test_wait_until_time_schedules_a_dormant_continuation(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    task = _seed_active_task(migrated_engine)
    cont = TaskContinuation(task_store=TaskStore(app_engine), queue=JobQueue(app_engine))
    cont.apply("user_a", _outcome(task, LegDisposition.CONTINUE, resume_at=_LATER), now=_NOW)

    fetched = TaskStore(app_engine).get("user_a", "t1")
    assert fetched.state == TaskState.WAITING
    assert fetched.wait_kind == WaitKind.UNTIL_TIME
    jobs = _leg_jobs(migrated_engine)
    assert len(jobs) == 1
    # Dormant: the continuation is QUEUED (not claimed/running) for the future instant.
    assert jobs[0]["state"] == "queued"
    assert jobs[0]["scheduled_at"] == _LATER


# --- waiting(on_user): zero-cost dormancy + the resume seam ------------------


def test_wait_on_user_is_zero_cost(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_active_task(migrated_engine)
    cont = TaskContinuation(task_store=TaskStore(app_engine), queue=JobQueue(app_engine))
    cont.wait_on_user("user_a", "t1", now=_NOW)
    fetched = TaskStore(app_engine).get("user_a", "t1")
    assert fetched.state == TaskState.WAITING
    assert fetched.wait_kind == WaitKind.ON_USER
    # Zero-cost: a state row and NO job — the reply will resume it.
    assert len(_leg_jobs(migrated_engine)) == 0


def test_resume_enqueues_leg_carrying_the_reply(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_active_task(migrated_engine)
    cont = TaskContinuation(task_store=TaskStore(app_engine), queue=JobQueue(app_engine))
    cont.wait_on_user("user_a", "t1", now=_NOW)
    cont.resume("user_a", "t1", UserReply(reply="yes, Tuesday works"), now=_NOW)
    jobs = _leg_jobs(migrated_engine)
    assert len(jobs) == 1
    trigger = jobs[0]["payload"]["trigger"]
    assert trigger["kind"] == "user_reply"
    assert trigger["reply"] == "yes, Tuesday works"  # the reply rides into the next leg
