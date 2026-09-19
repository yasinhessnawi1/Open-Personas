"""The untitled-conversation backfill: titling keyed on content, not on channel (issue #8).

The threshold trigger (:mod:`persona_api.services.title_trigger`) fires from a
WRITE: a turn finishes on the shared chat-turn worker and the crossing is
evaluated. That covers web chat and every connector, and persona-voice makes the
same enqueue for a call at session-end. What it cannot cover is a conversation
whose write path never reached a trigger at all: a call whose teardown died with
the process, a conversation grown only by proactive/originated messages, and
(the reason this exists) every conversation that was already sitting untitled in
the database before the floor came down to 2.

So this sweep asks the only question that has nothing to do with who wrote the
rows: *is this conversation still unnamed, and does it have enough content to
name?* Everything it finds is handed to the SAME durable ``title_refresh`` job
the triggers enqueue, with the SAME idempotency key shape, so there is one
titling mechanism and this is only another producer for it.

Cheap by construction: one grouped scan bounded by :data:`_DEFAULT_LIMIT`, and a
conversation drops out of the result the moment it has a title, so a finished
backlog costs one indexed query per pass and nothing else. Leader-gated on its
own advisory key, so exactly one worker sweeps however many run.
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING, Final

from persona.logging import get_logger
from sqlalchemy import func, select

from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.jobs.handlers.title_refresh import enqueue_title_refresh

if TYPE_CHECKING:
    from sqlalchemy import Engine, Select

    from persona_api.jobs.queue import JobQueue
    from persona_api.schedules.leadership import SchedulerLeader

__all__ = [
    "MIN_TITLE_MESSAGES",
    "TITLE_BACKFILL_LOCK_KEY",
    "UntitledConversationBackfill",
    "untitled_conversation_candidates",
]

_log = get_logger("api.services.title_backfill")

#: A stable 64-bit key for this sweep's leader lock, distinct from every other sweep's.
TITLE_BACKFILL_LOCK_KEY: Final[int] = zlib.crc32(b"persona:title:backfill:leader")

#: The least content worth naming: one completed exchange. The same floor the
#: trigger's first threshold uses, so the two producers agree about what "enough
#: to title" means and an empty conversation is left alone by both.
MIN_TITLE_MESSAGES: Final[int] = 2

#: Roles whose text the title handler reads. Counting anything else here would
#: send it a conversation whose transcript it then finds empty.
_TRANSCRIPT_ROLES: Final[tuple[str, ...]] = ("user", "assistant")

#: Candidates per pass. Bounded work per tick; the backlog drains over passes.
_DEFAULT_LIMIT: Final[int] = 200


def untitled_conversation_candidates(
    *, limit: int, min_messages: int
) -> Select[tuple[str, str, int]]:
    """The scan: untitled conversations with at least ``min_messages`` of transcript.

    Grouped rather than correlated so one pass over ``messages`` (which is
    indexed by ``conversation_id``) answers both halves: does it have content,
    and how much. The count is what keys the enqueue, so a conversation that grew
    since the last pass is a NEW job rather than a deduped-away no-op.

    Args:
        limit: Maximum candidates returned.
        min_messages: The content floor (:data:`MIN_TITLE_MESSAGES`).

    Returns:
        A select of ``(conversation_id, owner_id, message_count)``.
    """
    return (
        select(
            conversations_t.c.id.label("conversation_id"),
            conversations_t.c.owner_id.label("owner_id"),
            func.count(messages_t.c.id).label("message_count"),
        )
        .select_from(
            conversations_t.join(messages_t, messages_t.c.conversation_id == conversations_t.c.id)
        )
        .where(
            conversations_t.c.title == "",
            messages_t.c.role.in_(_TRANSCRIPT_ROLES),
            messages_t.c.content != "",
        )
        .group_by(conversations_t.c.id, conversations_t.c.owner_id)
        .having(func.count(messages_t.c.id) >= min_messages)
        .limit(limit)
    )


class UntitledConversationBackfill:
    """Enqueues a title refresh for every conversation still sitting unnamed.

    Args:
        dispatch_engine: The worker's cross-tenant engine. The scan reads every
            tenant's rows, exactly like the other worker sweeps.
        queue: The durable queue the ``title_refresh`` job is written to.
        leader: The leadership gate on :data:`TITLE_BACKFILL_LOCK_KEY`.
        limit: Maximum conversations enqueued per pass (bounded work).
        min_messages: The content floor; below it there is nothing to name.
    """

    def __init__(
        self,
        *,
        dispatch_engine: Engine,
        queue: JobQueue,
        leader: SchedulerLeader,
        limit: int = _DEFAULT_LIMIT,
        min_messages: int = MIN_TITLE_MESSAGES,
    ) -> None:
        self._dispatch = dispatch_engine
        self._queue = queue
        self._leader = leader
        self._limit = limit
        self._min_messages = min_messages

    def run_once(self) -> int:
        """Leader-gated: enqueue a refresh per untitled conversation. Returns how many.

        A conversation already carrying a queued job for the same count is A0's
        ``ON CONFLICT`` no-op and is not counted, so a repeated pass over an
        undrained backlog reports zero rather than re-reporting the same rows.
        """
        if not self._leader.try_become_leader():
            return 0  # a follower does no work; at most one backfiller runs
        stmt = untitled_conversation_candidates(limit=self._limit, min_messages=self._min_messages)
        with self._dispatch.begin() as conn:
            candidates = [
                (str(r.conversation_id), str(r.owner_id), int(r.message_count))
                for r in conn.execute(stmt)
            ]
        enqueued = 0
        for conversation_id, owner_id, message_count in candidates:
            record = enqueue_title_refresh(
                self._queue,
                owner_id=owner_id,
                conversation_id=conversation_id,
                threshold=message_count,
            )
            if record is not None:
                enqueued += 1
        if enqueued:
            _log.info(
                "title backfill enqueued refreshes for untitled conversations",
                enqueued=enqueued,
                scanned=len(candidates),
            )
        return enqueued
