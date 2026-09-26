"""A task that ends stops being paused, in the durable row (R9-158).

The task page reads ``tasks.paused`` to decide whether to offer Resume. The entity now
clears the overlay on every terminal transition; these drive the real ``TaskStore`` against
a real database and read the row itself back, because the defect was a row that said
``completed`` and ``paused`` at once, and the page believed the second half.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.tasks import Contract, Task, TaskState, WaitKind
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.db.models import tasks as tasks_t
from persona_api.tasks.store import TaskStore
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_finished"
_PERSONA = "persona_finished"
_TASK = "task-finished"
_NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "finished.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="f@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
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
            contract=Contract(goal="find three sources"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    s.start(_OWNER, _TASK, now=_NOW)
    return s


def _row(engine: Engine) -> tuple[str, bool, str | None]:
    with engine.begin() as conn:
        row = conn.execute(
            select(tasks_t.c.state, tasks_t.c.paused, tasks_t.c.wait_kind).where(
                tasks_t.c.id == _TASK
            )
        ).one()
    return (str(row.state), bool(row.paused), row.wait_kind)


def test_a_paused_task_that_completes_is_stored_unpaused(engine: Engine, store: TaskStore) -> None:
    store.pause(_OWNER, _TASK, now=_NOW)
    assert _row(engine) == (TaskState.ACTIVE.value, True, None)
    store.complete(_OWNER, _TASK, now=_NOW)
    assert _row(engine) == (TaskState.COMPLETED.value, False, None)


def test_a_paused_waiting_task_that_fails_is_stored_unpaused(
    engine: Engine, store: TaskStore
) -> None:
    store.begin_wait(_OWNER, _TASK, WaitKind.ON_USER, now=_NOW)
    store.pause(_OWNER, _TASK, now=_NOW)
    store.fail(_OWNER, _TASK, now=_NOW)
    assert _row(engine) == (TaskState.FAILED.value, False, None)


def test_a_finished_task_cannot_be_budget_unpaused(engine: Engine, store: TaskStore) -> None:
    # The budget extension's CAS clears ``paused`` WHERE it is set and then enqueues the next
    # leg. A completed task that kept ``paused=true`` would pass that CAS and record an
    # extension for work that is over. With the overlay cleared at the end, it no-ops.
    store.pause(_OWNER, _TASK, now=_NOW)
    store.complete(_OWNER, _TASK, now=_NOW)
    assert (store.cas_unpause(_OWNER, _TASK, now=_NOW), _row(engine)) == (
        False,
        (TaskState.COMPLETED.value, False, None),
    )
