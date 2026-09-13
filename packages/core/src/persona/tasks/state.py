"""The task lifecycle state machine (Spec A2, T2).

A task moves ``defined → active → waiting(kind) → active → … → completed | failed |
cancelled`` (spec §2). ``waiting`` is one state qualified by a :class:`WaitKind`; the
``paused`` overlay is orthogonal (a boolean on the entity, not a state here — D-A2-X-
core-api-split). Pure transition table mirroring the A0 ``Job`` state machine: legal
edges are data, illegal edges raise :class:`TaskStateError`.

See ``docs/specs/phase3/spec_A2/decisions.md`` (D-A2-X-core-api-split) and the spec §2
lifecycle.
"""

from __future__ import annotations

from enum import StrEnum

from persona.errors import TaskStateError

__all__ = [
    "TERMINAL_STATES",
    "TaskKind",
    "TaskState",
    "WaitKind",
    "can_transition",
    "is_terminal",
    "validate_transition",
]


class TaskKind(StrEnum):
    """What kind of work a task is (Spec W1, D-W1-2).

    ``STANDING`` is the A4/A8 shape: a contract the user confirmed, usually with a
    schedule or an event trigger behind it. ``AD_HOC`` is a one-off "just run this"
    dispatch: the same entity with a degenerate contract, so a bare run belongs to a task
    and is reachable from the work list (D-W1-1). The kind never changes after create.
    """

    STANDING = "standing"
    AD_HOC = "ad_hoc"


class TaskState(StrEnum):
    """The lifecycle state of a task.

    ``WAITING`` is qualified by a :class:`WaitKind` on the entity; the terminal trio
    (``COMPLETED`` / ``FAILED`` / ``CANCELLED``) admit no further transitions.
    """

    DEFINED = "defined"
    ACTIVE = "active"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WaitKind(StrEnum):
    """Why a task is waiting (spec §2).

    ``UNTIL_TIME`` rides A1's schedule (a self-scheduled continuation); ``ON_USER``
    parks on an approval or question (C0 delivers, C1's reply resumes); ``ON_EVENT``
    is the reserved seam (defined, no producer in v1 — D-A2-5).
    """

    UNTIL_TIME = "until_time"
    ON_USER = "on_user"
    ON_EVENT = "on_event"


#: The terminal states — no further transitions are legal.
TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
)

_VALID_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DEFINED: frozenset({TaskState.ACTIVE, TaskState.CANCELLED}),
    TaskState.ACTIVE: frozenset(
        {TaskState.WAITING, TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.WAITING: frozenset({TaskState.ACTIVE, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def is_terminal(state: TaskState) -> bool:
    """True if ``state`` admits no further transitions."""
    return state in TERMINAL_STATES


def can_transition(frm: TaskState, to: TaskState) -> bool:
    """True if ``frm → to`` is a legal edge in the lifecycle."""
    return to in _VALID_TRANSITIONS[frm]


def validate_transition(frm: TaskState, to: TaskState) -> None:
    """Raise :class:`TaskStateError` if ``frm → to`` is illegal."""
    if not can_transition(frm, to):
        raise TaskStateError(
            "illegal task state transition",
            context={"from": frm.value, "to": to.value},
        )
