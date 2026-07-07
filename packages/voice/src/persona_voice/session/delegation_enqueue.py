"""The voice-side delegated-turn enqueue — the twin raw-INSERT writer (Spec A9, A9-D-5).

persona-voice is a **peer process** to persona-api (it cannot import ``persona_api``), so on a
confirmed spoken task/schedule/autonomy ask it enqueues the durable ``delegated_turn`` job with its
own small raw INSERT — beside its raw-SQL peers (``enqueue_voice_synthesis``, ``_load_persona``,
``make_session_rls_engine``). The A0 ``jobs`` table stays api-owned; this is only a *writer*.

**Canonical semantics live in core** — ``persona.jobs.delegation`` (``DelegatedTurnPayload`` +
``delegated_turn_idempotency_key`` + ``make_delegated_turn_payload``): the api-side handler and
this writer build the *identical* payload + key, only the INSERT differs. Kept from drifting by:

* the **one canonical contract** in core (payload shape + key + job-type string);
* a **bidirectional column-parity test** (``tests/unit/session/test_delegation_enqueue.py``): this
  writer's columns ⊆ the api ``jobs`` Table **and** ⊇ its NOT-NULL-without-default columns — so a
  renamed/dropped column *and* a newly-added required column both fail CI statically;
* the **cross-layer real-transition test**: a job written here is claimed + executed by the **real
  A0 worker** (the enqueue→worker-drain→create chain — the synthetic-harness-real-transition rule).

The write is owner-scoped by the session RLS engine's checkout GUC (the engine is already pinned to
the caller — no ``rls_connection`` needed). This mirrors ``enqueue_voice_synthesis`` exactly; if you
edit either writer, update the twin (the parity test fails loudly if you forget).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from persona.jobs import (
    DELEGATED_TURN_JOB_TYPE,
    PROVENANCE_VOICE,
    delegated_turn_idempotency_key,
    make_delegated_turn_payload,
)
from persona.logging import get_logger
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["INSERTED_COLUMNS", "enqueue_delegated_turn"]

_logger = get_logger("voice.delegation_enqueue")

# The INSERT — the twin of ``JobQueue.enqueue``'s statement (and ``enqueue_voice_synthesis``). Only
# the NOT-NULL-without-default + payload columns are listed; every other jobs column (id/state/
# attempt/priority/max_attempts/scheduled_at/created_at) fills from its server default, as api's
# ``pg_insert(jobs).values(...)`` relies on. The payload is cast from a JSON string so no
# dict-adaptation is assumed. RETURNING id lets the caller (and the real-transition test) confirm a
# fresh enqueue vs an ``ON CONFLICT`` no-op.
_INSERT_SQL = text(
    """
    INSERT INTO jobs (type, owner_id, payload, idempotency_key)
    VALUES (:type, :owner_id, CAST(:payload AS JSONB), :idempotency_key)
    ON CONFLICT (owner_id, idempotency_key) DO NOTHING
    RETURNING id
    """
)

#: The columns this writer INSERTs — the parity test asserts this set against the real api ``jobs``
#: Table (⊆ all columns; ⊇ NOT-NULL-without-default columns).
INSERTED_COLUMNS: frozenset[str] = frozenset({"type", "owner_id", "payload", "idempotency_key"})


def enqueue_delegated_turn(
    engine: Engine,
    *,
    owner_id: str,
    conversation_id: str,
    verbatim_ask: str,
    persona_id: str,
    provenance: str = PROVENANCE_VOICE,
    dedup_token: str | None = None,
) -> str | None:
    """Enqueue a durable ``delegated_turn`` job for a confirmed spoken ask (A9-D-5).

    Builds the SAME core ``DelegatedTurnPayload`` + key the api-side re-enqueue would build (the
    verbatim ask, provenance ``voice``), then INSERTs the durable A0 job. Idempotent: a re-enqueue
    of the same ``(conversation_id, verbatim_ask)`` — a double-confirm / redelivery — is an
    ``ON CONFLICT`` no-op (draft-hash-primary; the ``dedup_token`` escape hatch overrides the ask
    hash for a deliberate same-phrase repeat).

    Args:
        engine: The session RLS engine (already owner-scoped via its checkout GUC).
        owner_id: The caller's user id (the job owner + RLS scope).
        conversation_id: The call's conversation id (the delegated turn runs in it).
        verbatim_ask: The verbatim spoken ask (never a parsed draft — the frontier re-parses it).
        persona_id: The persona on the call.
        provenance: The originating surface (``voice``) — recorded on the audit row.
        dedup_token: The A9-D-5 escape hatch (a gate-minted per-proposal token); ``None`` ⇒ the key
            hashes the verbatim ask.

    Returns:
        The enqueued job id, or ``None`` if an identical job already existed (the ``ON CONFLICT``
        no-op).
    """
    payload = make_delegated_turn_payload(
        conversation_id=conversation_id,
        verbatim_ask=verbatim_ask,
        persona_id=persona_id,
        provenance=provenance,
        dedup_token=dedup_token,
    )
    with engine.begin() as conn:
        row = conn.execute(
            _INSERT_SQL,
            {
                "type": DELEGATED_TURN_JOB_TYPE,
                "owner_id": owner_id,
                "payload": json.dumps(payload.model_dump()),
                "idempotency_key": delegated_turn_idempotency_key(payload),
            },
        ).first()
    job_id = row[0] if row is not None else None
    _logger.info(
        "voice delegated turn enqueued (conversation={cid} persona={pid} job={job})",
        cid=conversation_id,
        pid=persona_id,
        job=job_id or "<duplicate-noop>",
    )
    return job_id
