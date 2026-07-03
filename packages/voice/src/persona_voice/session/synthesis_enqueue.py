"""The voice-side synthesis enqueue — the twin raw-INSERT writer (Spec V13, D-4-amended).

persona-voice is a **peer process** to persona-api (it cannot import ``persona_api``),
so it enqueues the durable ``synthesis`` job with its own small raw INSERT — beside
its existing raw-SQL peers (``_load_persona``, ``make_session_rls_engine``). The A0
``jobs`` table stays api-owned; this is only a *writer*.

**Canonical semantics live in api's ``persona_api.jobs.queue.JobQueue.enqueue``** —
the same ``INSERT … ON CONFLICT (owner_id, idempotency_key) DO NOTHING`` over the same
table. This writer is its TWIN. The two are kept from drifting by:

* the **one canonical contract** in core — ``persona.jobs`` (``SynthesisJobPayload`` +
  ``synthesis_idempotency_key`` + ``make_conversation_synthesis_payload``): both writers
  build the *identical* payload + key, only the INSERT differs;
* a **bidirectional column-parity test** (``tests/unit/session/test_synthesis_enqueue.py``):
  this writer's columns ⊆ the api ``jobs`` Table **and** ⊇ its NOT-NULL-without-default
  columns — so a renamed/dropped column *and* a newly-added required column both fail CI
  statically, not at runtime;
* the **cross-layer real-transition test** (``tests/integration/…``): a job written here is
  claimed + processed by the **real A0 worker**.

If you edit either writer, update the twin (and the parity test will fail loudly if you
forget). The write is owner-scoped by the session RLS engine's checkout GUC (no
``rls_connection`` needed — the engine is already pinned to the caller).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from persona.jobs import (
    CHANNEL_VOICE,
    SYNTHESIS_JOB_TYPE,
    make_conversation_synthesis_payload,
    synthesis_idempotency_key,
)
from persona.logging import get_logger
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["enqueue_voice_synthesis"]

_logger = get_logger("voice.synthesis_enqueue")

# The INSERT — the twin of ``JobQueue.enqueue``'s statement. Only the four
# NOT-NULL-without-default + payload columns are listed; every other jobs column
# (id/state/attempt/priority/max_attempts/scheduled_at/created_at) fills from its
# server default, exactly as api's ``pg_insert(jobs).values(...)`` relies on. The
# payload is cast from a JSON string so no dict-adaptation is assumed. RETURNING id
# lets the caller (and the real-transition test) confirm a fresh enqueue vs a
# ``ON CONFLICT`` no-op.
_INSERT_SQL = text(
    """
    INSERT INTO jobs (type, owner_id, payload, idempotency_key)
    VALUES (:type, :owner_id, CAST(:payload AS JSONB), :idempotency_key)
    ON CONFLICT (owner_id, idempotency_key) DO NOTHING
    RETURNING id
    """
)

#: The columns this writer INSERTs — the parity test asserts this set against the
#: real api ``jobs`` Table (⊆ all columns; ⊇ NOT-NULL-without-default columns).
INSERTED_COLUMNS: frozenset[str] = frozenset({"type", "owner_id", "payload", "idempotency_key"})


def enqueue_voice_synthesis(
    engine: Engine,
    *,
    owner_id: str,
    conversation_id: str,
    persona_id: str,
    message_count: int,
) -> str | None:
    """Enqueue post-call graph synthesis for a voice conversation (V13-T5).

    Builds the SAME core ``SynthesisJobPayload`` the api writer builds (channel
    ``voice``, ``interaction_kind = conversation`` — voice persists to the messages
    table like chat), then INSERTs the durable A0 job. Idempotent: a re-enqueue of
    the same ``(conversation_id, message_count)`` is an ``ON CONFLICT`` no-op.

    Args:
        engine: The session RLS engine (already owner-scoped via its checkout GUC).
        owner_id: The caller's user id (the job owner).
        conversation_id: The call's conversation id (the synthesis interaction id).
        persona_id: The persona on the call.
        message_count: The final message count (the high-water-mark / idempotency scope).

    Returns:
        The enqueued job id, or ``None`` if an identical job already existed
        (the ``ON CONFLICT`` no-op).
    """
    payload = make_conversation_synthesis_payload(
        conversation_id=conversation_id,
        persona_id=persona_id,
        message_count=message_count,
        channel=CHANNEL_VOICE,
    )
    with engine.begin() as conn:
        row = conn.execute(
            _INSERT_SQL,
            {
                "type": SYNTHESIS_JOB_TYPE,
                "owner_id": owner_id,
                "payload": json.dumps(payload.model_dump()),
                "idempotency_key": synthesis_idempotency_key(payload),
            },
        ).first()
    job_id = row[0] if row is not None else None
    _logger.info(
        "voice synthesis enqueued (conversation={cid} msgs={n} job={job})",
        cid=conversation_id,
        n=message_count,
        job=job_id or "<duplicate-noop>",
    )
    return job_id
