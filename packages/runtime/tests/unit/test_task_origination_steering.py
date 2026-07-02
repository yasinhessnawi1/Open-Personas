"""Unit tests for steering cue detection + intent (Spec A4, T9b)."""

from __future__ import annotations

import pytest
from persona_runtime.task_origination import SteeringIntent, SteeringVerb, detect_steering_cue

_STEERING = [
    "pause the fare check",
    "hold off on that task",
    "stop watching the listing",
    "resume my morning digest",
    "start it again",
    "cancel the fare task",
    "call it off",
    "never mind that task",
    "drop the appeal task",
]

_NOT_STEERING = [
    "how's it going with the fares?",
    "what tasks am I running?",
    "summarise this article",
    "thanks, that's great",
]


@pytest.mark.parametrize("message", _STEERING)
def test_steering_phrasings_fire_a_cue(message: str) -> None:
    assert detect_steering_cue(message) is True


@pytest.mark.parametrize("message", _NOT_STEERING)
def test_non_steering_phrasings_do_not_fire(message: str) -> None:
    assert detect_steering_cue(message) is False


def test_steering_intent_is_frozen() -> None:
    intent = SteeringIntent(verb=SteeringVerb.CANCEL, task_id="t1")
    assert intent.verb is SteeringVerb.CANCEL
    assert intent.task_id == "t1"
