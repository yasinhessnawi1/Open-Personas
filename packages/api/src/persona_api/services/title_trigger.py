"""Title-refresh trigger producer — enqueue at conversation growth boundaries (R9-020).

The channel-agnostic seam (the ``synthesis_trigger`` shape): the web chat
turn-end enqueues the durable ``title_refresh`` job here; a voice session-end
call-site is a later one-liner against the SAME producer (out of the R9-020
batch, by design). Off the critical path; a duplicate is A0's ``ON CONFLICT``
no-op via the ``title:{conversation_id}:{threshold}`` key.

A refresh fires when the conversation's message TOTAL crosses one of
:data:`TITLE_REFRESH_THRESHOLDS` — "crosses" as in ``previous < T <= new``, so
the trigger is parity-robust (a chat turn adds two rows, but a voice-born or
nudge-carrying conversation may sit on any count) and never re-fires between
thresholds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from persona_api.jobs.handlers.title_refresh import enqueue_title_refresh

if TYPE_CHECKING:
    from persona_api.jobs.queue import JobQueue

__all__ = [
    "TITLE_REFRESH_THRESHOLDS",
    "crossed_title_threshold",
    "enqueue_conversation_title_refresh",
]


TITLE_REFRESH_THRESHOLDS: Final[tuple[int, ...]] = (4, 10, 24, 50, 100)
"""Message totals at which the title is regenerated over the whole transcript.

Front-loaded (4 → 10 → 24) because the early turns are where a first-message
title goes stale fastest; sparse later (50, 100) because a long conversation's
subject is stable. One durable job per crossing, ever, per conversation.
"""


def crossed_title_threshold(*, previous_count: int, new_count: int) -> int | None:
    """The highest threshold crossed by growing ``previous_count → new_count``.

    Returns ``None`` when no threshold lies in ``(previous_count, new_count]``
    — in particular for every turn BETWEEN thresholds (no re-fire) and for any
    non-growing input. Half-open on the left so sitting exactly ON a threshold
    does not re-fire it on the next turn.
    """
    if new_count <= previous_count:
        return None
    for threshold in reversed(TITLE_REFRESH_THRESHOLDS):
        if previous_count < threshold <= new_count:
            return threshold
    return None


def enqueue_conversation_title_refresh(
    queue: JobQueue | None,
    *,
    owner_id: str,
    conversation_id: str,
    previous_count: int,
    new_count: int,
) -> None:
    """Enqueue a title refresh iff this growth step crossed a threshold.

    No-op when ``queue`` is ``None`` (the queue-not-configured path — CLI /
    unit tests) or when no threshold was crossed. Channel-agnostic: the chat
    turn worker calls it at the clean-completion boundary; voice adds its
    session-end call later against this same seam.
    """
    if queue is None:
        return
    threshold = crossed_title_threshold(previous_count=previous_count, new_count=new_count)
    if threshold is None:
        return
    enqueue_title_refresh(
        queue,
        owner_id=owner_id,
        conversation_id=conversation_id,
        threshold=threshold,
    )
