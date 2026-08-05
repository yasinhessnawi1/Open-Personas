"""Pausing a task must pause its schedule too (R9-108).

`TaskStore.pause` set only the task's `paused` overlay. The leg handler honours
that and skips the work, so nothing runs -- but the SCHEDULE kept firing on
cadence. Production: 47 fires against a paused task, each enqueueing a job that
was immediately discarded, while the calendar still presented the task as live.
The owner reasonably read that as "pause did nothing".

These drive the REAL mirror against a real database and assert the schedule row
itself, because the defect was precisely that two rows disagreed.

The mirror deliberately lives at the route layer and goes through
`ScheduleStore`'s own API, not `TaskStore`: A10-D-9 permits exactly ONE schedule
write path, and adding a second one would be the very drift being fixed here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.tasks import Contract, Task
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.db.models import schedules as schedules_t
from persona_api.routes.tasks import _mirror_schedule_pause
from persona_api.tasks.store import TaskStore
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_pause"
_PERSONA = "persona_pause"
_TASK = "task-pause"
_SCHEDULE = "sched-pause"
_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "pause.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="p@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
        conn.execute(
            insert(schedules_t).values(
                id=_SCHEDULE,
                owner_id=_OWNER,
                timezone="UTC",
                recurrence="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
                target_job_type="task_scheduled_fire",
                payload_template={"task_id": _TASK},
                enabled=True,
                paused=False,
            )
        )
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine: Engine) -> TaskStore:
    s = TaskStore(engine)
    s.create(
        Task(
            id=_TASK,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="brief me"),
            schedule_id=_SCHEDULE,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    s.start(_OWNER, _TASK, now=_NOW)
    return s


def _schedule_paused(engine: Engine) -> bool:
    with engine.begin() as conn:
        return bool(
            conn.execute(
                select(schedules_t.c.paused).where(schedules_t.c.id == _SCHEDULE)
            ).scalar_one()
        )


def test_pausing_a_task_pauses_its_schedule(engine: Engine, store: TaskStore) -> None:
    """THE regression: a paused task must stop FIRING, not just stop working."""
    assert _schedule_paused(engine) is False
    task = store.pause(_OWNER, _TASK, now=_NOW)
    _mirror_schedule_pause(engine, _OWNER, task, paused=True)
    assert _schedule_paused(engine) is True, (
        "the task is paused but its schedule still fires -- this is the 47-fire "
        "runaway the owner saw, and why the calendar still showed the task as live"
    )


def test_unpausing_resumes_the_schedule(engine: Engine, store: TaskStore) -> None:
    """The mirror must be symmetric, or unpause would leave the task stuck."""
    _mirror_schedule_pause(engine, _OWNER, store.pause(_OWNER, _TASK, now=_NOW), paused=True)
    assert _schedule_paused(engine) is True
    _mirror_schedule_pause(engine, _OWNER, store.unpause(_OWNER, _TASK, now=_NOW), paused=False)
    assert _schedule_paused(engine) is False, "unpause must let the schedule fire again"


def test_a_task_without_a_schedule_pauses_cleanly(engine: Engine) -> None:
    """Not every task is scheduled; the mirror must not require one."""
    store = TaskStore(engine)
    store.create(
        Task(
            id="task-adhoc",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="one-off"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    store.start(_OWNER, "task-adhoc", now=_NOW)
    paused = store.pause(_OWNER, "task-adhoc", now=_NOW)
    _mirror_schedule_pause(engine, _OWNER, paused, paused=True)  # must not raise
    assert paused.paused is True
    # The unrelated schedule is untouched.
    assert _schedule_paused(engine) is False


def test_cancelling_a_task_also_stops_its_schedule(engine: Engine, store: TaskStore) -> None:
    """A cancelled task firing on cadence is the same runaway wearing a different hat.

    Cancel is terminal, so the leg handler skips just as it does for paused --
    and the schedule would likewise keep firing forever without the mirror.
    """
    task = store.cancel(_OWNER, _TASK, now=_NOW)
    _mirror_schedule_pause(engine, _OWNER, task, paused=True)
    with engine.begin() as conn:
        row = conn.execute(
            select(schedules_t.c.paused, schedules_t.c.enabled).where(schedules_t.c.id == _SCHEDULE)
        ).one()
    assert row.paused is True or row.enabled is False, (
        "a cancelled task must stop firing; leaving its schedule live re-creates the runaway"
    )
