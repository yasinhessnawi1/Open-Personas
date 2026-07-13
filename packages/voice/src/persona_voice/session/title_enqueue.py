"""The voice-side title-refresh enqueue — the twin raw-INSERT writer (R9-028).

persona-voice is a **peer process** to persona-api (it cannot import
``persona_api``), so it enqueues the durable ``title_refresh`` job with its own
small raw INSERT — the ``persona_voice.session.synthesis_enqueue`` V13
D-4-amended precedent, byte-for-byte. The A0 ``jobs`` table stays api-owned;
this is only a *writer*.

**Canonical semantics live in api's ``persona_api.jobs.queue.JobQueue.enqueue``**
— the same ``INSERT … ON CONFLICT (owner_id, idempotency_key) DO NOTHING`` over
the same table. This writer is its TWIN. The two are kept from drifting by:

* the **one canonical contract** in core — ``persona.jobs.title``
  (``TitleRefreshJobPayload`` + ``title_refresh_idempotency_key``): both writers
  build the *identical* payload + key, only the INSERT differs;
* a **bidirectional column-parity test** (``tests/unit/session/
  test_title_enqueue.py``): this writer's columns ⊆ the api ``jobs`` Table
  **and** ⊇ its NOT-NULL-without-default columns;
* the **cross-layer real-transition test**
  (``packages/api/tests/integration/test_voice_title_wired.py``): a job written
  here is claimed + processed by the **real A0 worker**.

R9-020's web trigger fires at message-count thresholds ``{4, 10, 24, 50, 100}``
— which a short call may never cross. So this writer fires ONCE, unconditionally,
at call session-end (regardless of the final message count): the idempotency key
is keyed on that final count, so a re-run of teardown (or a race with a
threshold the web trigger already crossed mid-call) is a safe ``ON CONFLICT``
no-op, never a duplicate regen.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from persona.jobs import (
    TITLE_REFRESH_JOB_TYPE,
    TitleRefreshJobPayload,
    title_refresh_idempotency_key,
)
from persona.logging import get_logger
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["enqueue_voice_title_refresh"]

_logger = get_logger("voice.title_enqueue")

# The INSERT — the twin of ``JobQueue.enqueue``'s statement. Only the four
# NOT-NULL-without-default + payload columns are listed; every other jobs column
# fills from its server default, exactly as api's ``pg_insert(jobs).values(...)``
# relies on. RETURNING id lets the caller confirm a fresh enqueue vs a
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


def enqueue_voice_title_refresh(
    engine: Engine,
    *,
    owner_id: str,
    conversation_id: str,
    message_count: int,
) -> str | None:
    """Enqueue one title refresh at call session-end (R9-028's voice leg).

    Builds the SAME core ``TitleRefreshJobPayload`` the api writer builds
    (``threshold`` set to the call's final ``message_count`` — not one of the
    web trigger's fixed crossings, just this writer's dedup scope), then
    INSERTs the durable A0 job. Idempotent: a re-enqueue of the same
    ``(conversation_id, message_count)`` is an ``ON CONFLICT`` no-op.

    Args:
        engine: The session RLS engine (already owner-scoped via its checkout
            GUC — the V9 ``CallRecorder`` / synthesis-enqueue precedent: a
            FRESH short-lived engine minted at teardown, since the session
            engine may already be disposed).
        owner_id: The caller's user id (the job owner).
        conversation_id: The call's conversation id.
        message_count: The final message count at session-end — fires
            REGARDLESS of value (never gated on a minimum), so even a
            two-line call gets titled.

    Returns:
        The enqueued job id, or ``None`` if an identical job already existed
        (the ``ON CONFLICT`` no-op).
    """
    payload = TitleRefreshJobPayload(conversation_id=conversation_id, threshold=message_count)
    with engine.begin() as conn:
        row = conn.execute(
            _INSERT_SQL,
            {
                "type": TITLE_REFRESH_JOB_TYPE,
                "owner_id": owner_id,
                "payload": json.dumps(payload.model_dump()),
                "idempotency_key": title_refresh_idempotency_key(payload),
            },
        ).first()
    job_id = row[0] if row is not None else None
    _logger.info(
        "voice title refresh enqueued (conversation={cid} msgs={n} job={job})",
        cid=conversation_id,
        n=message_count,
        job=job_id or "<duplicate-noop>",
    )
    return job_id
