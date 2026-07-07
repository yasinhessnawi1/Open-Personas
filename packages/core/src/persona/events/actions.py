"""The two — and only two — trigger action doors (Spec A7, A7-D-5, criterion 2).

A matched trigger has **exactly two** allowed consequences, each already passing an existing
consent/safety gate:

- :class:`FireTaskLeg` — fire a confirmed contract task's leg (A4's machinery; the event replaces
  the clock in the A1→A2 bridge). Carries only the confirmed ``task_id``.
- :class:`EnqueueInitiativeCandidate` — enter A5's pipeline (grounding→envelope→restraint→K4,
  unchanged) with the event as grounding. Carries no parameters in v1 — the candidate is derived
  from the event at dispatch time (A7-D-7 grounds on the event's existing citation).

There is deliberately no third variant ("run arbitrary tool on event" is the named non-goal). The
structural "no third door" proof (criterion 2) asserts :data:`TRIGGER_ACTION_KINDS` stays a
two-element set and the dispatcher exhaustively handles exactly these.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TRIGGER_ACTION_KINDS",
    "EnqueueInitiativeCandidate",
    "FireTaskLeg",
    "TriggerAction",
]


class FireTaskLeg(BaseModel):
    """Door (a): fire a confirmed contract task's leg (the A1→A2 bridge with an ``EventFire``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["fire_task_leg"] = "fire_task_leg"
    #: The confirmed contract task whose next leg the event fires (A4 echo-confirm created it).
    task_id: str = Field(min_length=1)


class EnqueueInitiativeCandidate(BaseModel):
    """Door (b): enqueue an A5 initiative candidate grounded on the event (unchanged pipeline)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["enqueue_candidate"] = "enqueue_candidate"


#: A matched trigger's action — the closed two-door union (Pydantic keys on ``kind``).
TriggerAction = Annotated[
    FireTaskLeg | EnqueueInitiativeCandidate,
    Field(discriminator="kind"),
]

#: The closed set of action kinds — a third entry here is a spec change, and the criterion-2
#: structural test asserts this set is exactly these two.
TRIGGER_ACTION_KINDS: frozenset[str] = frozenset({"fire_task_leg", "enqueue_candidate"})
