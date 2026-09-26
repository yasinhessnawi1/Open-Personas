"""Unit tests for the Task entity (Spec A2, T2).

The durable entity above runs: identity, the A4-authored contract, the lifecycle state
(+ the ``paused`` overlay + the ``WaitKind``), the cost ledger, the monotonic
checkpoint-sequence anchor (the A2-R-4 CAS predecessor), and the A4/A6 linkage points.
Transitions are pure functional updates (a new frozen Task), mirroring the A0 ``Job``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.errors import TaskStateError
from persona.tasks import (
    TASK_SCHEMA_VERSION,
    Contract,
    CostLedger,
    SpendKind,
    Task,
    TaskState,
    WaitKind,
)
from pydantic import ValidationError

_T0 = datetime(2026, 6, 24, 9, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 24, 10, 0, tzinfo=UTC)


def _task(**overrides: object) -> Task:
    base: dict[str, object] = {
        "id": "task-1",
        "owner_id": "owner-1",
        "persona_id": "persona-1",
        "contract": Contract(goal="find the cheapest fare"),
        "created_at": _T0,
        "updated_at": _T0,
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


# --- shape + defaults ---------------------------------------------------------


def test_minimal_task_defaults() -> None:
    task = _task()
    assert task.state == TaskState.DEFINED
    assert task.paused is False
    assert task.wait_kind is None
    assert task.ledger == CostLedger()
    assert task.head_checkpoint_seq is None
    assert task.run_ids == ()
    assert task.conversation_id is None
    assert task.workspace_id is None
    assert task.schedule_id is None
    assert task.schema_version == TASK_SCHEMA_VERSION


def test_task_is_frozen_and_forbids_extra() -> None:
    task = _task()
    with pytest.raises(ValidationError):
        task.state = TaskState.ACTIVE  # type: ignore[misc]
    with pytest.raises(ValidationError):
        _task(surprise="no")


def test_naive_timestamps_rejected() -> None:
    with pytest.raises(ValidationError):
        _task(created_at=datetime(2026, 6, 24, 9, 0))  # noqa: DTZ001 — naive on purpose


# --- the wait_kind invariant: set iff WAITING --------------------------------


def test_waiting_requires_a_wait_kind() -> None:
    with pytest.raises(ValidationError):
        _task(state=TaskState.WAITING)  # wait_kind missing


def test_non_waiting_forbids_a_wait_kind() -> None:
    with pytest.raises(ValidationError):
        _task(state=TaskState.ACTIVE, wait_kind=WaitKind.ON_USER)


# --- lifecycle transitions ----------------------------------------------------


def test_start_moves_defined_to_active() -> None:
    task = _task().start(now=_T1)
    assert task.state == TaskState.ACTIVE
    assert task.updated_at == _T1


def test_start_only_from_defined() -> None:
    active = _task().start(now=_T1)
    with pytest.raises(TaskStateError):
        active.start(now=_T1)


def test_begin_wait_sets_state_and_kind() -> None:
    task = _task().start(now=_T1).begin_wait(WaitKind.UNTIL_TIME, now=_T1)
    assert task.state == TaskState.WAITING
    assert task.wait_kind == WaitKind.UNTIL_TIME


def test_begin_wait_only_from_active() -> None:
    with pytest.raises(TaskStateError):
        _task().begin_wait(WaitKind.ON_USER, now=_T1)


def test_resume_clears_wait_kind() -> None:
    waiting = _task().start(now=_T1).begin_wait(WaitKind.ON_USER, now=_T1)
    resumed = waiting.resume(now=_T1)
    assert resumed.state == TaskState.ACTIVE
    assert resumed.wait_kind is None


def test_resume_only_from_waiting() -> None:
    with pytest.raises(TaskStateError):
        _task().start(now=_T1).resume(now=_T1)


def test_resume_rejected_from_defined() -> None:
    # DEFINED→ACTIVE is a legal *edge* (start uses it), but resume is a different
    # operation — it must require WAITING, or a never-started task could "resume".
    with pytest.raises(TaskStateError):
        _task().resume(now=_T1)


def test_start_rejected_from_waiting() -> None:
    # Symmetric: WAITING→ACTIVE is resume's edge; start must require DEFINED.
    waiting = _task().start(now=_T1).begin_wait(WaitKind.ON_USER, now=_T1)
    with pytest.raises(TaskStateError):
        waiting.start(now=_T1)


def test_complete_from_active() -> None:
    done = _task().start(now=_T1).complete(now=_T1)
    assert done.state == TaskState.COMPLETED


def test_fail_from_active_and_waiting() -> None:
    assert _task().start(now=_T1).fail(now=_T1).state == TaskState.FAILED
    waiting = _task().start(now=_T1).begin_wait(WaitKind.ON_USER, now=_T1)
    failed = waiting.fail(now=_T1)
    assert failed.state == TaskState.FAILED
    assert failed.wait_kind is None  # leaving WAITING clears the kind


def test_cancel_from_any_non_terminal() -> None:
    assert _task().cancel(now=_T1).state == TaskState.CANCELLED  # from DEFINED
    assert _task().start(now=_T1).cancel(now=_T1).state == TaskState.CANCELLED
    waiting = _task().start(now=_T1).begin_wait(WaitKind.ON_USER, now=_T1)
    assert waiting.cancel(now=_T1).state == TaskState.CANCELLED


def test_cancel_terminal_is_rejected() -> None:
    done = _task().start(now=_T1).complete(now=_T1)
    with pytest.raises(TaskStateError):
        done.cancel(now=_T1)


# --- the paused overlay (orthogonal to the lifecycle state) ------------------


def test_pause_sets_overlay_without_changing_state() -> None:
    active = _task().start(now=_T1)
    paused = active.pause(now=_T1)
    assert paused.paused is True
    assert paused.state == TaskState.ACTIVE  # underlying state preserved


def test_unpause_clears_overlay() -> None:
    paused = _task().start(now=_T1).pause(now=_T1)
    assert paused.unpause(now=_T1).paused is False


def test_pause_rejected_when_already_paused_or_terminal() -> None:
    paused = _task().start(now=_T1).pause(now=_T1)
    with pytest.raises(TaskStateError):
        paused.pause(now=_T1)
    done = _task().start(now=_T1).complete(now=_T1)
    with pytest.raises(TaskStateError):
        done.pause(now=_T1)


def test_unpause_rejected_when_not_paused() -> None:
    with pytest.raises(TaskStateError):
        _task().start(now=_T1).unpause(now=_T1)


def test_cancel_while_paused_clears_overlay() -> None:
    paused = _task().start(now=_T1).pause(now=_T1)
    cancelled = paused.cancel(now=_T1)
    assert cancelled.state == TaskState.CANCELLED
    assert cancelled.paused is False


# R9-158: a pause holds work that is left. A task that ends has none, so every terminal
# transition clears the hold, or a finished task goes on presenting as paused with Resume
# and Cancel offered (the owner ruled a pause pressed while a leg finishes the work
# completes the task, 2026-09-26).


def test_completing_a_paused_task_ends_it_unpaused() -> None:
    done = _task().start(now=_T0).pause(now=_T0).complete(now=_T1)
    assert (done.state, done.paused, done.wait_kind, done.updated_at) == (
        TaskState.COMPLETED,
        False,
        None,
        _T1,
    )


def test_failing_a_paused_active_task_ends_it_unpaused() -> None:
    failed = _task().start(now=_T0).pause(now=_T0).fail(now=_T1)
    assert (failed.state, failed.paused, failed.wait_kind) == (TaskState.FAILED, False, None)


def test_failing_a_paused_waiting_task_ends_it_unpaused() -> None:
    waiting = _task().start(now=_T0).begin_wait(WaitKind.ON_USER, now=_T0).pause(now=_T0)
    failed = waiting.fail(now=_T1)
    assert (failed.state, failed.paused, failed.wait_kind) == (TaskState.FAILED, False, None)


def test_an_unpaused_task_that_ends_stays_unpaused() -> None:
    active = _task().start(now=_T0)
    ended = (active.complete(now=_T1), active.fail(now=_T1), active.cancel(now=_T1))
    assert [(t.state, t.paused) for t in ended] == [
        (TaskState.COMPLETED, False),
        (TaskState.FAILED, False),
        (TaskState.CANCELLED, False),
    ]


# --- cost ledger accounting ---------------------------------------------------


def test_record_spend_accumulates_into_the_ledger() -> None:
    task = _task().start(now=_T1).record_spend(SpendKind.MODEL, 1200, now=_T1)
    assert task.ledger.model_micros == 1200
    assert task.ledger.total_micros == 1200
    assert task.updated_at == _T1


def test_record_spend_rejected_on_terminal() -> None:
    done = _task().start(now=_T1).complete(now=_T1)
    with pytest.raises(TaskStateError):
        done.record_spend(SpendKind.MODEL, 100, now=_T1)


# --- the checkpoint-sequence anchor (A2-R-4 CAS predecessor) ------------------


def test_next_checkpoint_seq_starts_at_zero() -> None:
    assert _task().next_checkpoint_seq == 0


def test_advance_checkpoint_is_monotonic_successor() -> None:
    task = _task().start(now=_T1)
    task = task.advance_checkpoint(0, now=_T1)
    assert task.head_checkpoint_seq == 0
    assert task.next_checkpoint_seq == 1
    task = task.advance_checkpoint(1, now=_T1)
    assert task.head_checkpoint_seq == 1
    assert task.next_checkpoint_seq == 2


def test_advance_checkpoint_rejects_non_successor() -> None:
    task = _task().start(now=_T1)
    with pytest.raises(TaskStateError):
        task.advance_checkpoint(1, now=_T1)  # expected 0
    task = task.advance_checkpoint(0, now=_T1)
    with pytest.raises(TaskStateError):
        task.advance_checkpoint(0, now=_T1)  # re-append the same seq — store no-ops; entity rejects
    with pytest.raises(TaskStateError):
        task.advance_checkpoint(5, now=_T1)  # gap


def test_advance_checkpoint_rejected_on_terminal() -> None:
    done = _task().start(now=_T1).complete(now=_T1)
    with pytest.raises(TaskStateError):
        done.advance_checkpoint(0, now=_T1)


# --- kind (Spec W1, D-W1-2) ----------------------------------------------------


def test_task_kind_defaults_to_standing() -> None:
    from persona.tasks import TaskKind

    assert _task().kind is TaskKind.STANDING


def test_ad_hoc_kind_round_trips_and_survives_transitions() -> None:
    from persona.tasks import TaskKind

    task = _task(kind=TaskKind.AD_HOC)
    assert task.kind is TaskKind.AD_HOC
    # The kind is fixed at create: a lifecycle transition carries it unchanged.
    assert task.start(now=_T1).kind is TaskKind.AD_HOC
    # And it survives a JSON round trip (the serde the api store relies on).
    assert Task.model_validate(task.model_dump(mode="json")).kind is TaskKind.AD_HOC


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _task(kind="scheduled")
