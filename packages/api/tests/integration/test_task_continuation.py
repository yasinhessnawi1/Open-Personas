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
from persona_api.tasks.store import CheckpointStore
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


def test_on_state_change_fires_task_updated_on_terminal_post_write(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Spec A11/A6 (W8): the injected signal fires once on a terminal transition, AFTER the write.

    Surface-lags-truth: the ping fires after the state row is written, so a refetch reads truth.
    The waiting(on_user) path calls the same ``_signal`` seam (proven by ``test_wait_on_user_*``).
    """
    signals: list[tuple[str, str, str]] = []
    task = _seed_active_task(migrated_engine)
    cont = TaskContinuation(
        task_store=TaskStore(app_engine),
        queue=JobQueue(app_engine),
        on_state_change=lambda owner, tid, state: signals.append((owner, tid, state)),
    )
    cont.apply("user_a", _outcome(task, LegDisposition.COMPLETED), now=_NOW)
    assert signals == [("user_a", "t1", TaskState.COMPLETED.value)]  # terminal, exactly once
    assert TaskStore(app_engine).get("user_a", "t1").state == TaskState.COMPLETED


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


# --- R9-163: a park records WHAT the task is blocked on ----------------------


def _blocked_reason(engine: Engine, task_id: str = "t1") -> str | None:
    """The head checkpoint's obstacle — what every surface actually reads."""
    head = CheckpointStore(engine).get_latest("user_a", task_id)
    return None if head is None else head.blocked_on


def _gate_outcome(task: Task, blocked_on: str) -> LegOutcome:
    """What the executor hands back when a leg hits the A3 approval gate: no checkpoint."""
    return LegOutcome(
        task=task,
        checkpoint=None,
        run=None,
        disposition=LegDisposition.WAITING_APPROVAL,
        box_limit=None,
        spend={},
        proposal_id="prop_1",
        blocked_on=blocked_on,
    )


def test_a_stuck_park_leaves_the_head_exactly_where_the_dead_job_left_it(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The stuck park records no obstacle checkpoint, and that is deliberate.

    The head checkpoint sequence is the correlation key between a task and the job that died
    on it: the revival sweep finds a transient failure by matching a dead job whose key is
    ``task:{id}:after:{head}``. A park that appended anything would move the head off that
    value, the dead row would stop matching, and transient failures would silently never be
    picked up on the user's behalf again. An earlier draft did exactly that.
    """
    task = _seed_active_task(migrated_engine)
    checkpoints = CheckpointStore(app_engine)
    cont = TaskContinuation(
        task_store=TaskStore(app_engine), queue=JobQueue(app_engine), checkpoint_store=checkpoints
    )
    checkpoints.append(
        task,
        TaskCheckpoint(
            task_id="t1",
            leg_id="t1:leg:0",
            checkpoint_seq=0,
            progress_conclusions=("the deposit is held at Sparebanken",),
            updated_at=_NOW,
        ),
        now=_NOW,
    )

    report = cont.react_to_dead_leg("user_a", "t1", "the portal rejected the login", now=_NOW)

    assert report is not None
    assert report.cause == "the portal rejected the login"  # the cause is still carried, voiced
    fetched = TaskStore(app_engine).get("user_a", "t1")
    assert fetched.state == TaskState.WAITING
    assert fetched.head_checkpoint_seq == 0  # unmoved — the dead job's key still matches


def test_the_obstacle_carries_the_prior_progress_forward(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """It is a checkpoint, so the head still answers "where did it get to"."""
    task = _seed_active_task(migrated_engine)
    checkpoints = CheckpointStore(app_engine)
    checkpoints.append(
        task,
        TaskCheckpoint(
            task_id="t1",
            leg_id="t1:leg:0",
            checkpoint_seq=0,
            progress_conclusions=("the deposit is held at Sparebanken",),
            next_step="call the branch",
            updated_at=_NOW,
        ),
        now=_NOW,
    )
    cont = TaskContinuation(
        task_store=TaskStore(app_engine), queue=JobQueue(app_engine), checkpoint_store=checkpoints
    )

    cont.apply(
        "user_a",
        _gate_outcome(
            TaskStore(app_engine).get("user_a", "t1"),
            "Waiting for your approval: Call the branch on your behalf",
        ),
        now=_NOW,
    )

    head = checkpoints.get_latest("user_a", "t1")
    assert head is not None
    assert head.checkpoint_seq == 1
    assert head.progress_conclusions == ("the deposit is held at Sparebanken",)
    assert head.next_step == "call the branch"
    assert head.blocked_on == "Waiting for your approval: Call the branch on your behalf"


def test_an_ordinary_leg_clears_the_obstacle(migrated_engine: Engine, app_engine: Engine) -> None:
    """Nobody has to remember to erase it: a leg that ran writes no ``blocked_on``."""
    task = _seed_active_task(migrated_engine)
    checkpoints = CheckpointStore(app_engine)
    tasks = TaskStore(app_engine)
    cont = TaskContinuation(
        task_store=tasks, queue=JobQueue(app_engine), checkpoint_store=checkpoints
    )
    cont.apply("user_a", _gate_outcome(task, "Waiting for your approval: Send the email"), now=_NOW)
    assert _blocked_reason(app_engine) == "Waiting for your approval: Send the email"

    resumed = tasks.resume("user_a", "t1", now=_NOW)
    checkpoints.append(
        resumed,
        TaskCheckpoint(
            task_id="t1",
            leg_id="t1:leg:1",
            checkpoint_seq=resumed.next_checkpoint_seq,
            progress_conclusions=("the email went out",),
            updated_at=_NOW,
        ),
        now=_NOW,
    )

    assert _blocked_reason(app_engine) is None


def test_an_approval_park_names_what_it_waits_for(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """A gated leg wrote no checkpoint at all, so the task waited for an unnamed something."""
    task = _seed_active_task(migrated_engine)
    tasks = TaskStore(app_engine)
    cont = TaskContinuation(
        task_store=tasks, queue=JobQueue(app_engine), checkpoint_store=CheckpointStore(app_engine)
    )
    gated = _gate_outcome(task, "Waiting for your approval: Send an email to the landlord")

    cont.apply("user_a", gated, now=_NOW)

    fetched = tasks.get("user_a", "t1")
    assert fetched.state == TaskState.WAITING
    assert fetched.wait_kind == WaitKind.ON_USER
    assert _blocked_reason(app_engine) == "Waiting for your approval: Send an email to the landlord"
    assert len(_leg_jobs(migrated_engine)) == 0  # still zero-cost: a state row, no job


def test_a_question_park_is_not_an_obstacle(migrated_engine: Engine, app_engine: Engine) -> None:
    """The distinction the field turns on: a leg that worked and then asked is not blocked.

    The waiting state already says it waits and ``open_questions`` already carries the
    question. Calling that 'blocked' would make the continuity window report a leg that did
    a full leg's work as a leg that hit a wall.
    """
    task = _seed_active_task(migrated_engine)
    checkpoints = CheckpointStore(app_engine)
    cont = TaskContinuation(
        task_store=TaskStore(app_engine), queue=JobQueue(app_engine), checkpoint_store=checkpoints
    )
    checkpoints.append(
        task,
        TaskCheckpoint(
            task_id="t1",
            leg_id="t1:leg:0",
            checkpoint_seq=0,
            progress_conclusions=("three clinics have Tuesday slots",),
            open_questions=("Which clinic, and which day?",),
            updated_at=_NOW,
        ),
        now=_NOW,
    )
    # The leg's own append already advanced the head, so the outcome carries the post-append
    # task (what the executor really hands the continuation) rather than the shared helper's
    # always-seq-0 shape.
    asked = LegOutcome(
        task=TaskStore(app_engine).get("user_a", "t1"),
        checkpoint=checkpoints.get_latest("user_a", "t1"),
        run=None,
        disposition=LegDisposition.WAITING_USER,
        box_limit=None,
        spend={},
    )

    cont.apply("user_a", asked, now=_NOW)

    head = checkpoints.get_latest("user_a", "t1")
    assert head is not None
    assert head.blocked_on is None
    assert head.open_questions == ("Which clinic, and which day?",)
