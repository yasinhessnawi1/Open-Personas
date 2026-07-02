"""Steering a live task by saying so — pause / resume / cancel (Spec A4, T9b; criterion 8).

The other half of conversational steering: a user controls a *running* task in chat ("pause the
fare check", "cancel that", "start it again"). The confirm posture differs by verb, on purpose:

- **pause / resume** are reversible and low-stakes → they apply immediately, no confirmation;
- **cancel** is irreversible (a standing/spending task is killed) → it requires a light,
  **consequence-aware** confirmation first ("this stops the daily fare check — cancel it?"), so a
  misheard "cancel" can never silently end a task. The clean-confirm bar is the same one origination
  uses (:func:`is_affirmative_confirmation`).

Which task is resolved by **list-then-resolve** over the introspection reader's active tasks (the
same surface the persona introspects with) — the model picks the one the user meant; an ambiguous
or unmatched reference yields no intent (the loop asks rather than guessing).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from persona.tasks import TaskSummary

__all__ = [
    "SteeringIntent",
    "SteeringInterpreter",
    "SteeringVerb",
    "detect_steering_cue",
]


class SteeringVerb(StrEnum):
    """The steering action the user asked for."""

    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


class SteeringIntent(BaseModel):
    """A resolved steering request: the verb + the concrete task it targets."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: SteeringVerb
    task_id: str


@runtime_checkable
class SteeringInterpreter(Protocol):
    """The model-backed step that resolves a steering message to a verb + a specific task.

    Returns ``None`` when the message is not a steering request, or when the target task can't be
    resolved from the active list (the loop then asks rather than steering the wrong task).
    """

    def interpret(
        self, message: str, active_tasks: Sequence[TaskSummary]
    ) -> Awaitable[SteeringIntent | None]:
        """Resolve ``message`` against the caller's ``active_tasks`` (or ``None``)."""
        ...


# Cheap, high-recall cue: does the message plausibly steer a task? The interpreter decides.
_STEERING_CUE = re.compile(
    r"\b("
    r"pause|hold\s+off|stop\s+(watching|tracking|the|doing|that|it)|"  # pause-ish
    r"resume|unpause|start\s+(it\s+)?(again|back)|pick\s+it\s+back\s+up|"  # resume-ish
    r"cancel|call\s+it\s+off|never\s+mind\s+(the|that)|"  # cancel-ish
    r"drop\s+(the|that)|forget\s+(the|that)"
    r")\b",
    re.IGNORECASE,
)


def detect_steering_cue(message: str) -> bool:
    """High-recall trigger for a steering request (the interpreter is the precision layer)."""
    return _STEERING_CUE.search(message) is not None
