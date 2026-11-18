"""A task transition writes only over the state it read (R9-247).

``TaskStore._transition`` read the row, applied the entity transition and wrote the result
back by id alone. A second writer that committed between the read and the write was
overwritten: a Pause that read ACTIVE, then the leg's ``complete``, then Pause's write stored
``(active, paused)`` over the completed task, and a leg's ``begin_wait`` could wipe out a
pause the user had just pressed. The write now compares what it read, and a lost race
re-reads and decides again through the entity's own guards.

Each race is made deterministic the same way: the transition's ``apply`` commits the
competing write once, through a second store, before it returns, so the competitor lands
exactly between this transition's read and its write.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.errors import TaskStateError
from persona.tasks import Contract, Task, TaskState, WaitKind
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.tasks import TaskStore
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(seconds=1)
_OWNER = "user_cas"
_PERSONA = "persona_cas"
_TASK = "t_cas"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "cas.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="c@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


def _active_task(store: TaskStore) -> Task:
    store.create(
        Task(
            id=_TASK,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="find a flat"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    return store.start(_OWNER, _TASK, now=_NOW)


def _racing(
    competitor: Callable[[], object], transition: Callable[[Task], Task]
) -> Callable[[Task], Task]:
    """An ``apply`` that lets ``competitor`` commit once, between the read and the write."""
    fired = False

    def apply(task: Task) -> Task:
        nonlocal fired
        if not fired:
            fired = True
            competitor()
        return transition(task)

    return apply


def test_a_pause_that_loses_to_a_completion_does_not_reopen_the_task(engine: Engine) -> None:
    """Pause reads ACTIVE, the leg completes, then Pause writes: the completion stands."""
    store = TaskStore(engine)
    _active_task(store)
    other = TaskStore(engine)

    with pytest.raises(TaskStateError):
        store._transition(
            _OWNER,
            _TASK,
            "task.pause",
            _racing(
                lambda: other.complete(_OWNER, _TASK, now=_LATER),
                lambda t: t.pause(now=_LATER),
            ),
        )

    final = store.get(_OWNER, _TASK)
    assert (final.state, final.paused) == (TaskState.COMPLETED, False)


def test_a_park_that_loses_to_a_pause_keeps_the_pause(engine: Engine) -> None:
    """The leg's ``begin_wait`` reads unpaused, the user pauses, then the park writes."""
    store = TaskStore(engine)
    _active_task(store)
    other = TaskStore(engine)

    parked = store._transition(
        _OWNER,
        _TASK,
        "task.begin_wait",
        _racing(
            lambda: other.pause(_OWNER, _TASK, now=_LATER),
            lambda t: t.begin_wait(WaitKind.ON_USER, now=_LATER),
        ),
    )

    final = store.get(_OWNER, _TASK)
    assert (final.state, final.wait_kind, final.paused) == (
        TaskState.WAITING,
        WaitKind.ON_USER,
        True,
    )
    assert parked.paused is True


def test_a_completion_that_loses_to_a_cancel_does_not_undo_the_cancel(engine: Engine) -> None:
    """The leg's ``complete`` reads ACTIVE, the user's Cancel lands, then the completion writes.
    A read-modify-write stored COMPLETED over the cancel the user had just pressed. The compare
    misses, the completion decides again on the CANCELLED row, and the entity refuses."""
    store = TaskStore(engine)
    _active_task(store)
    other = TaskStore(engine)

    with pytest.raises(TaskStateError):
        store._transition(
            _OWNER,
            _TASK,
            "task.complete",
            _racing(
                lambda: other.cancel(_OWNER, _TASK, now=_LATER),
                lambda t: t.complete(now=_LATER),
            ),
        )

    assert store.get(_OWNER, _TASK).state is TaskState.CANCELLED


def test_a_pause_racing_a_change_of_wait_keeps_the_new_wait(engine: Engine) -> None:
    """The wait kind is part of what is compared. A Pause reads WAITING(until_time); before it
    writes, the task resumes and parks again on a question, WAITING(on_user), so state and
    pause match what Pause read and only the wait kind differs. Comparing state and pause alone
    let Pause write its stale ``until_time`` back over the question the task now waits on."""
    store = TaskStore(engine)
    _active_task(store)
    store.begin_wait(_OWNER, _TASK, WaitKind.UNTIL_TIME, now=_NOW)
    other = TaskStore(engine)

    def _resumed_and_asked() -> None:
        other.resume(_OWNER, _TASK, now=_LATER)
        other.begin_wait(_OWNER, _TASK, WaitKind.ON_USER, now=_LATER)

    store._transition(
        _OWNER,
        _TASK,
        "task.pause",
        _racing(_resumed_and_asked, lambda t: t.pause(now=_LATER)),
    )

    final = store.get(_OWNER, _TASK)
    assert (final.state, final.wait_kind, final.paused) == (
        TaskState.WAITING,
        WaitKind.ON_USER,
        True,
    )


def test_a_cancel_that_loses_to_a_completion_reports_the_task_finished(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real race through the cancel door: the cancel's transition reads ACTIVE, the leg's
    completion commits, then the cancel writes. Before the compare-and-set the cancel landed
    over the completion; now the completion stands and the door answers that the task had
    already finished (``None``) instead of raising a server error."""
    from persona_api.services import task_control_service

    store = TaskStore(engine)
    live = _active_task(store)
    other = TaskStore(engine)
    original_cancel = Task.cancel
    fired = False

    def _cancel_after_the_leg_completes(self: Task, *, now: datetime) -> Task:
        nonlocal fired
        if not fired:
            fired = True
            other.complete(_OWNER, _TASK, now=_LATER)
        return original_cancel(self, now=now)

    monkeypatch.setattr(Task, "cancel", _cancel_after_the_leg_completes)

    summary = task_control_service.cancel_task(engine, _OWNER, live, now=_LATER)

    assert summary is None
    final = store.get(_OWNER, _TASK)
    assert (final.state, final.paused) == (TaskState.COMPLETED, False)


# --- F: the pause door says what actually stopped it ------------------------------------


def test_a_pause_that_loses_to_a_pause_says_already_paused(engine: Engine) -> None:
    """The door's snapshot is unpaused, but another Pause landed first. The transition then
    meets an already-paused row; that is "Already paused.", not "already finished"."""
    from persona_api.services import task_control_service

    store = TaskStore(engine)
    stale = _active_task(store)
    store.pause(_OWNER, _TASK, now=_LATER)

    outcome = task_control_service.pause_task(engine, _OWNER, stale, now=_LATER)

    assert (outcome.changed, outcome.note) == (False, "Already paused.")


def test_a_pause_whose_task_kept_changing_says_so(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every compare misses (the row changes under each attempt), so the transition gives up.
    The task is live and unpaused: the honest answer is to try again, not "already finished"."""
    from persona_api.services import task_control_service

    store = TaskStore(engine)
    live = _active_task(store)
    monkeypatch.setattr(TaskStore, "_persist_state", lambda *_a, **_k: False)

    outcome = task_control_service.pause_task(engine, _OWNER, live, now=_LATER)

    assert (outcome.changed, outcome.note) == (False, task_control_service.PAUSE_RACED_NOTE)
