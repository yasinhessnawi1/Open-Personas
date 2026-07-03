"""The conversational reschedule verb — retime/rerule a live task in chat (Spec A8, T6).

The A8 extension of A4's steering seam: a user changes a *confirmed* task's cadence by saying so
("move the morning brief to 9", "make it weekly", "skip tomorrow's"). Unlike pause/resume (which
apply immediately, steering.py), a reschedule is a **propose→confirm** flow — the persona re-echoes
the FULL new schedule clause in the user's timezone and applies only on an explicit yes, through
the one CAS-guarded door (A8-D-7/D-2).

Target resolution is honest (bar 1): the target is resolved by list-then-resolve over the active
tasks (the same reader the persona introspects with); an ambiguous reference lists the candidates
and ASKS (never guesses), and an unresolvable one is a plain miss. The new cadence parses through
the SAME parser as create (:mod:`schedule`) — no second grammar; an unrepresentable cadence declines
honestly. Quiet-hours (A8-D-6) warn-plus-offer wires into the echo here.
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 — datetime is a runtime Pydantic field type
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from persona.tasks import TaskSummary

__all__ = [
    "RescheduleIntent",
    "RescheduleInterpreter",
    "RescheduleResolution",
    "RescheduleResolutionKind",
    "detect_reschedule_cue",
    "render_proposal_echo",
    "render_reschedule_echo",
]


class RescheduleResolutionKind(StrEnum):
    """How a reschedule message resolved against the active tasks."""

    RESOLVED = "resolved"  # a single clear target — proceed to parse the new cadence + echo
    AMBIGUOUS = "ambiguous"  # several plausible targets — list them and ASK (never guess)
    NOT_FOUND = "not_found"  # no matching task / not a reschedule — a plain, honest miss


class RescheduleIntent(BaseModel):
    """A resolved reschedule: the target task + the new cadence candidate (or a skip-next)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    #: An A1-shaped RRULE candidate the same parser as create validates (XOR ``one_time_at``).
    recurrence_rrule: str | None = None
    #: A one-time future instant (XOR ``recurrence_rrule``).
    one_time_at: datetime | None = None
    #: "skip tomorrow's" — suppress exactly the next occurrence, no cadence change (A8-D-2).
    skip_next: bool = False


class RescheduleResolution(BaseModel):
    """The interpreter's verdict: resolved intent, an ambiguous candidate list, or a miss."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RescheduleResolutionKind
    intent: RescheduleIntent | None = None
    #: For ``AMBIGUOUS`` — the goals of the plausible targets, for the persona's list-and-ask.
    candidate_goals: tuple[str, ...] = ()


@runtime_checkable
class RescheduleInterpreter(Protocol):
    """The model-backed step that resolves a reschedule message to a target + new cadence.

    Honest by construction: a single clear match → ``RESOLVED``; several plausible matches →
    ``AMBIGUOUS`` (the loop lists + asks); nothing resolvable → ``NOT_FOUND`` (the loop declines).
    Never guesses a target it is unsure of.
    """

    def interpret(
        self, message: str, active_tasks: Sequence[TaskSummary]
    ) -> Awaitable[RescheduleResolution]:
        """Resolve ``message`` against the caller's ``active_tasks``."""
        ...


# Cheap, high-recall cue: does the message plausibly retime/rerule a task? The interpreter decides.
_RESCHEDULE_CUE = re.compile(
    r"\b("
    r"reschedule|re-schedule|"
    r"move\s+(it|the|that|my)|"
    r"make\s+it\s+(daily|weekly|monthly|every|at|\d)|"
    r"change\s+(it|the|that).*\bto\b|"
    r"(switch|shift)\s+(it|the|that)|"
    r"instead\s+of|"
    r"skip\s+(tomorrow|the\s+next|today)|"
    r"push\s+(it|the|that)\s+(to|back)|"
    r"earlier|later"
    r")\b",
    re.IGNORECASE,
)


def detect_reschedule_cue(message: str) -> bool:
    """High-recall trigger for a reschedule request (the interpreter is the precision layer)."""
    return _RESCHEDULE_CUE.search(message) is not None


def render_reschedule_echo(
    *,
    task_goal: str,
    human_terms: str,
    timezone: str,
    next_fire_phrase: str,
    quiet_hours_offer: str | None = None,
) -> str:
    """Re-echo the FULL new schedule clause in the user's tz + next-fire preview (A8-D-2).

    The A4 honesty pattern applied to a reschedule: the user confirms the WHOLE new "When:"
    (never a diff fragment). ``quiet_hours_offer`` (A8-D-6), when set, appends a warn + the nearest
    edge as an alternative the user may take or override — it never silently shifts the time. The
    echo invites only what is wired (a plain yes/no confirm).
    """
    clause = f'For "{task_goal}" — When: {human_terms} · {timezone} — next run {next_fire_phrase}.'
    if quiet_hours_offer is not None:
        clause += f" (That's inside your quiet hours — want {quiet_hours_offer} instead?)"
    return f"{clause} Apply that?"


def render_proposal_echo(*, task_goal: str, reason: str, human_terms: str, timezone: str) -> str:
    """The persona's voiced reschedule PROPOSAL — propose-first, never a silent apply (A8-D-12).

    The persona surfaces a context-driven suggestion (a quiet-hours collision, repeated failures)
    as a plain yes/no question: it states the reason, the proposed cadence in human terms (no raw
    RRULE), and asks. It applies ONLY on the user's confirm through the CAS door — this copy invites
    exactly that one wired capability (the A4 echo rule), nothing it cannot do.
    """
    return f'{reason} Want me to move "{task_goal}" to {human_terms} · {timezone}?'
