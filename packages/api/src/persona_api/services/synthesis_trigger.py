"""Synthesis trigger producers — enqueue at interaction boundaries (Spec K2, T8d-producer).

The channel-agnostic seam (the C1 forward seam): a web turn-end, an agentic-run
completion, and a voice session-end all enqueue the same durable A0 ``synthesis``
job (D-K2-2). These thin producers wrap :func:`enqueue_synthesis` with the
channel → ``InteractionKind`` mapping and a queue-absent no-op, so the call sites
(``chat_service`` turn-end, ``run_worker`` run-end, the voice session-end) stay a
single additive line — reconciled additively at merge-back (P1's turn-end sink is
additive to K2's turn-end enqueue). Off the critical path; a duplicate is A0's
``ON CONFLICT`` no-op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.extraction import InteractionKind
from persona.stores.lifecycle import EpisodicSettings

from persona_api.jobs.handlers.episodic_consolidation import enqueue_episodic_consolidation
from persona_api.jobs.handlers.synthesis import enqueue_synthesis

if TYPE_CHECKING:
    from persona_api.jobs.queue import JobQueue

__all__ = ["enqueue_conversation_synthesis", "enqueue_run_synthesis"]


def _enqueue_episodic(queue: JobQueue, *, owner_id: str, persona_id: str) -> None:
    """The K8 turn-tail trigger: the same boundary that dirties memory schedules
    the sleep-time engine (idle-deferred, bucket-coalesced; kill-switch gated).

    Settings are env-read per call (cheap pydantic-settings construction, no
    globals); ``engine_enabled=False`` means NO job is ever enqueued — the
    built-but-inert guard's trigger half (the handler half gates registration).
    """
    settings = EpisodicSettings()
    if not settings.engine_enabled:
        return
    enqueue_episodic_consolidation(
        queue,
        owner_id=owner_id,
        persona_id=persona_id,
        delay_seconds=settings.idle_delay_seconds,
        bucket_seconds=settings.bucket_seconds,
    )


def enqueue_conversation_synthesis(
    queue: JobQueue | None,
    *,
    owner_id: str,
    conversation_id: str,
    persona_id: str,
    message_count: int,
) -> None:
    """Enqueue synthesis at a web/voice conversation boundary (turn-end / session-end).

    No-op when ``queue`` is ``None`` (the queue-not-configured path — CLI / tests).
    ``message_count`` scopes the idempotency key so a continued conversation re-keys
    to a new job and a re-enqueue of the same turn is an A0 no-op.
    """
    if queue is None:
        return
    enqueue_synthesis(
        queue,
        owner_id=owner_id,
        interaction_kind=InteractionKind.CONVERSATION.value,
        interaction_id=conversation_id,
        persona_id=persona_id,
        message_count=message_count,
    )
    _enqueue_episodic(queue, owner_id=owner_id, persona_id=persona_id)


def enqueue_run_synthesis(
    queue: JobQueue | None,
    *,
    owner_id: str,
    run_id: str,
    persona_id: str,
) -> None:
    """Enqueue synthesis at agentic-run completion (Spec 06 D-06-8 metadata feeds it).

    A run is one atomic unit, so the high-water-mark is a constant ``1`` — a re-run
    over the same run dedups via the marker + the A0 key. No-op without a queue.
    """
    if queue is None:
        return
    enqueue_synthesis(
        queue,
        owner_id=owner_id,
        interaction_kind=InteractionKind.AGENTIC_RUN.value,
        interaction_id=run_id,
        persona_id=persona_id,
        message_count=1,
    )
    _enqueue_episodic(queue, owner_id=owner_id, persona_id=persona_id)
