"""The ``conversation_memory_forget`` job: the durable half of forgetting a conversation (R9-311).

Enqueued in the transaction that deletes the conversation's row with ``forget_memory``, with
the ids of its episodic chunks (:func:`persona_api.services.conversation_memory_forget.record`).
The request forgets them at once; this job is what makes that certain. If the request's forget
failed, or the process stopped before it ran, the job removes the chunks and what was derived
from them, and the queue retries it until it succeeds. A run after a successful forget removes
nothing: every step of the forget is idempotent.

It runs in the job owner's scope (the worker's RLS chokepoint), like every tenant job, so the
memory transport sees only the owner's personas. It forgets only what its owner forgot: the
conversation must be marked forgotten for this owner and persona, else it does nothing. The
mark goes with its persona, so a job left over from a persona since erased does nothing either.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from persona.jobs import SHORT_LEASE, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from persona.stores.episodic import EpisodicStore

from persona_api.jobs.handlers.avatar import JobTypeCarrier
from persona_api.services.conversation_memory_forget import (
    JOB_MAX_ATTEMPTS,
    JOB_TYPE,
    ConversationMemoryForgetPayload,
    idempotency_key,
    is_forgotten,
    remove,
)

if TYPE_CHECKING:
    from persona.audit import AuditLogger
    from persona.jobs import JobContext, JobRegistry
    from persona.stores.backend import Backend

__all__ = [
    "ConversationMemoryForgetHandler",
    "conversation_memory_forget_queue_available",
    "register_conversation_memory_forget_handler",
]

_log = get_logger("jobs.conversation_memory_forget")


class ConversationMemoryForgetHandler:
    """Forgets one forgotten conversation's episodic chunks. Idempotent."""

    def __init__(self, *, backend: Backend, audit_logger: AuditLogger) -> None:
        self._backend = backend
        self._audit_logger = audit_logger

    async def handle(self, payload: ConversationMemoryForgetPayload, context: JobContext) -> None:
        with context.connection() as conn:
            forgotten = is_forgotten(
                conn,
                owner_id=context.owner_id,
                persona_id=payload.persona_id,
                conversation_id=payload.conversation_id,
            )
        if not forgotten:
            _log.info(
                "conversation memory forget skipped: not forgotten by this owner",
                owner_id=context.owner_id,
                conversation_id=payload.conversation_id,
            )
            return
        store = EpisodicStore(backend=self._backend, audit_logger=self._audit_logger)
        # Off the event loop: the store is synchronous. The owner's scope travels with the
        # copied context.
        removed = await asyncio.to_thread(remove, store, payload.persona_id, payload.chunk_ids)
        _log.info(
            "conversation memory forget ran",
            owner_id=context.owner_id,
            conversation_id=payload.conversation_id,
            chunks=removed,
        )


def register_conversation_memory_forget_handler(
    registry: JobRegistry, *, backend: Backend, audit_logger: AuditLogger
) -> None:
    """Register the tenant on the worker's owner-scoped memory transport."""
    registry.register(
        JobTypeSpec(
            type=JOB_TYPE,
            payload_model=ConversationMemoryForgetPayload,
            handler=ConversationMemoryForgetHandler(backend=backend, audit_logger=audit_logger),
            idempotency_key=idempotency_key,
            retry=RetryPolicy(
                max_attempts=JOB_MAX_ATTEMPTS, base_backoff_seconds=10.0, max_backoff_seconds=3600.0
            ),
            lease=SHORT_LEASE,
        )
    )


def conversation_memory_forget_queue_available(
    *, job_queue: object | None, in_process_worker: object | None
) -> bool:
    """Whether a worker in this process runs the forget job; else the forget runs first.

    Asking the worker what it registered keeps "enqueue implies handler" structural (R9-013):
    a forget is never left in a queue no worker here will run.
    """
    if job_queue is None or not isinstance(in_process_worker, JobTypeCarrier):
        return False
    return JOB_TYPE in in_process_worker.job_types
