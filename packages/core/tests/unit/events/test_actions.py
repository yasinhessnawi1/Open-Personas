"""Unit tests for the two-door action union (Spec A7, A7-D-5, criterion 2)."""

from __future__ import annotations

import typing

import pytest
from persona.events import (
    TRIGGER_ACTION_KINDS,
    EnqueueInitiativeCandidate,
    FireTaskLeg,
    TriggerAction,
)
from persona.events.actions import TriggerAction as _TriggerActionAnnotated
from pydantic import TypeAdapter, ValidationError


def test_there_are_exactly_two_doors() -> None:
    # The type-level half of the criterion-2 "no third door" proof: the union has exactly the two
    # allowed variants, and the declared kind set matches them. A third door is a compile-time add
    # here that this test rejects.
    args = typing.get_args(typing.get_args(_TriggerActionAnnotated)[0])
    assert set(args) == {FireTaskLeg, EnqueueInitiativeCandidate}
    assert set(TRIGGER_ACTION_KINDS) == {"fire_task_leg", "enqueue_candidate"}


def test_fire_task_leg_carries_the_confirmed_task() -> None:
    action = FireTaskLeg(task_id="task-1")
    assert action.kind == "fire_task_leg"
    assert action.task_id == "task-1"


def test_enqueue_candidate_carries_no_parameters() -> None:
    action = EnqueueInitiativeCandidate()
    assert action.kind == "enqueue_candidate"


def test_actions_are_frozen_and_forbid_extra() -> None:
    action = FireTaskLeg(task_id="t")
    with pytest.raises(ValidationError):
        action.task_id = "z"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        FireTaskLeg(task_id="t", run_tool="rm -rf /")  # type: ignore[call-arg]


def test_action_is_a_discriminated_union() -> None:
    adapter: TypeAdapter[TriggerAction] = TypeAdapter(TriggerAction)
    assert isinstance(
        adapter.validate_python({"kind": "fire_task_leg", "task_id": "t"}), FireTaskLeg
    )
    assert isinstance(
        adapter.validate_python({"kind": "enqueue_candidate"}), EnqueueInitiativeCandidate
    )
    with pytest.raises(ValidationError):  # no third door parses
        adapter.validate_python({"kind": "run_tool", "cmd": "x"})
