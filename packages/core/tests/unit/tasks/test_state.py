"""Unit tests for the task state machine (Spec A2, T2).

The lifecycle: ``defined → active → waiting(kind) → active → … → completed |
failed | cancelled`` (D-A2-X-core-api-split). ``WaitKind`` gets its home here.
Pure transition table — no entity, no clock.
"""

from __future__ import annotations

import pytest
from persona.errors import TaskStateError
from persona.tasks import (
    TaskState,
    WaitKind,
    can_transition,
    is_terminal,
    validate_transition,
)


def test_wait_kind_values() -> None:
    assert WaitKind.UNTIL_TIME == "until_time"
    assert WaitKind.ON_USER == "on_user"
    assert WaitKind.ON_EVENT == "on_event"


def test_task_state_values() -> None:
    assert {s.value for s in TaskState} == {
        "defined",
        "active",
        "waiting",
        "completed",
        "failed",
        "cancelled",
    }


@pytest.mark.parametrize(
    "state",
    [TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED],
)
def test_terminal_states(state: TaskState) -> None:
    assert is_terminal(state)


@pytest.mark.parametrize(
    "state",
    [TaskState.DEFINED, TaskState.ACTIVE, TaskState.WAITING],
)
def test_non_terminal_states(state: TaskState) -> None:
    assert not is_terminal(state)


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (TaskState.DEFINED, TaskState.ACTIVE),
        (TaskState.DEFINED, TaskState.CANCELLED),
        (TaskState.ACTIVE, TaskState.WAITING),
        (TaskState.ACTIVE, TaskState.COMPLETED),
        (TaskState.ACTIVE, TaskState.FAILED),
        (TaskState.ACTIVE, TaskState.CANCELLED),
        (TaskState.WAITING, TaskState.ACTIVE),
        (TaskState.WAITING, TaskState.FAILED),
        (TaskState.WAITING, TaskState.CANCELLED),
    ],
)
def test_legal_transitions(frm: TaskState, to: TaskState) -> None:
    assert can_transition(frm, to)
    validate_transition(frm, to)  # no raise


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (TaskState.DEFINED, TaskState.WAITING),  # must start first
        (TaskState.DEFINED, TaskState.COMPLETED),
        (TaskState.WAITING, TaskState.COMPLETED),  # resume to active first
        (TaskState.COMPLETED, TaskState.ACTIVE),  # terminal is terminal
        (TaskState.FAILED, TaskState.ACTIVE),
        (TaskState.CANCELLED, TaskState.ACTIVE),
        (TaskState.ACTIVE, TaskState.DEFINED),  # no going back
    ],
)
def test_illegal_transitions(frm: TaskState, to: TaskState) -> None:
    assert not can_transition(frm, to)
    with pytest.raises(TaskStateError):
        validate_transition(frm, to)


def test_validate_transition_error_carries_context() -> None:
    with pytest.raises(TaskStateError) as exc:
        validate_transition(TaskState.COMPLETED, TaskState.ACTIVE)
    assert exc.value.context["from"] == "completed"
    assert exc.value.context["to"] == "active"


def test_task_kind_values() -> None:
    from persona.tasks import TaskKind

    assert {k.value for k in TaskKind} == {"standing", "ad_hoc"}
