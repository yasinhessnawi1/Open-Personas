"""What wrote a checkpoint: a leg, or a record a park or a person made (R9-220).

Every checkpoint carries a ``leg_id`` of the shape ``{task_id}:{kind}:{seq}``. Only one kind
is a leg that went at the work; the others record something that happened between legs:

- ``leg``: a leg ran and wrote its own checkpoint (the executor).
- ``blocked``: a park recorded what the task waits on (the approval gate, a contract bound).
- ``approval``: an approval was answered and its result folded in (the resolver).
- ``reply``: a reply arrived while the task could not run, and was saved for its next leg.

The head sequence counts all of them, so it is not the number of legs that ran. The leg cap
counts legs only, the same rule billing uses (D-ORCH-11-18-d (1)). The kind is minted and
read here, in one place, so the four writers and the counter cannot drift apart.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["CheckpointKind", "checkpoint_id", "checkpoint_id_prefix", "checkpoint_kind"]


class CheckpointKind(StrEnum):
    """Who wrote a checkpoint, as it appears in the middle of its ``leg_id``."""

    LEG = "leg"
    BLOCKED = "blocked"
    APPROVAL = "approval"
    REPLY = "reply"


def checkpoint_id(task_id: str, kind: CheckpointKind, seq: int) -> str:
    """The ``leg_id`` a writer of ``kind`` stamps on checkpoint ``seq`` of ``task_id``."""
    return f"{checkpoint_id_prefix(task_id, kind)}{seq}"


def checkpoint_id_prefix(task_id: str, kind: CheckpointKind) -> str:
    """Everything in a ``kind`` checkpoint's ``leg_id`` before its sequence number."""
    return f"{task_id}:{kind.value}:"


def checkpoint_kind(task_id: str, leg_id: str) -> CheckpointKind | None:
    """Which kind wrote ``leg_id`` on ``task_id``, or ``None`` for a shape no writer mints."""
    for kind in CheckpointKind:
        prefix = checkpoint_id_prefix(task_id, kind)
        if leg_id.startswith(prefix) and leg_id[len(prefix) :].isdigit():
            return kind
    return None
