"""The voice-side auto-top-up enqueue — the twin raw-INSERT writer (Spec M5, D-M5-15).

persona-voice is a **peer process** to persona-api (it cannot import ``persona_api``), and
an auto-top-up needs Stripe credentials the voice process must never hold. So when a
per-turn deduct crosses the low-balance line, voice does what A9 established for every
cross-process action it cannot perform itself: it enqueues a durable job with its own
small raw INSERT, beside its raw-SQL peers (``enqueue_delegated_turn``,
``enqueue_voice_synthesis``). The A0 ``jobs`` table stays api-owned; this is only a
*writer*.

**Canonical semantics live in core** — ``persona.jobs.autotopup`` (``AutoTopupPayload`` +
``auto_topup_idempotency_key`` + ``make_auto_topup_payload``): the api-side handler and
this writer build the *identical* payload + key, only the INSERT differs. Kept from
drifting by the same two guards A9 uses:

* the **one canonical contract** in core (payload shape + key + job-type string);
* a **bidirectional column-parity test**: this writer's columns ⊆ the api ``jobs`` Table
  **and** ⊇ its NOT-NULL-without-default columns, so a renamed/dropped column *and* a
  newly-added required column both fail CI statically;
* the **cross-layer real-transition test**: a job written here is claimed + executed by
  the **real** A0 worker, which then really calls ``maybe_auto_topup``. A test that
  invoked the handler directly would prove the handler works while proving nothing about
  whether the job ever reaches it.

The write is owner-scoped by the session RLS engine's checkout GUC (the engine is already
pinned to the caller), exactly like its twins. If you edit either writer, update the other
— the parity test fails loudly if you forget.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from persona.jobs import (
    AUTO_TOPUP_JOB_TYPE,
    auto_topup_idempotency_key,
    make_auto_topup_payload,
)
from persona.logging import get_logger
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["INSERTED_COLUMNS", "enqueue_auto_topup"]

_logger = get_logger("voice.topup_enqueue")

#: The INSERT — the twin of ``JobQueue.enqueue``'s statement (and ``enqueue_delegated_turn``).
#: Only the NOT-NULL-without-default + payload columns are listed; every other jobs column
#: fills from its server default. ``ON CONFLICT`` makes a re-fired trigger for the same
#: crossing a no-op, which is the FIRST of the four dedup layers (D-M5-19).
_INSERT_SQL = text(
    """
    INSERT INTO jobs (type, owner_id, payload, idempotency_key)
    VALUES (:type, :owner_id, CAST(:payload AS JSONB), :idempotency_key)
    ON CONFLICT (owner_id, idempotency_key) DO NOTHING
    RETURNING id
    """
)

#: The columns this writer INSERTs — the parity test asserts this set against the real api
#: ``jobs`` Table (⊆ all columns; ⊇ NOT-NULL-without-default columns).
INSERTED_COLUMNS: frozenset[str] = frozenset({"type", "owner_id", "payload", "idempotency_key"})


def enqueue_auto_topup(
    engine: Engine,
    *,
    owner_id: str,
    old_balance: int,
    new_balance: int,
    call_id: str,
    turn_seq: int,
    source: str = "voice",
) -> str | None:
    """Enqueue a durable auto-top-up trigger for an observed balance crossing (D-M5-15).

    Reports two BALANCES and nothing else: whether they constitute a crossing, whether the
    caller is eligible, and how much to charge are all the api's to decide (D-M5-16), so
    the voice process never learns what "$2" means.

    Args:
        engine: The session RLS engine (already owner-scoped via its checkout GUC).
        owner_id: The caller's user id (the job owner + RLS scope).
        old_balance: The balance before the deduct that prompted this.
        new_balance: The balance after it.
        call_id: The per-call-unique session id (never the conversation id — that would
            collide triggers across separate calls of one conversation).
        turn_seq: The turn within the call.
        source: The observing surface, for log attribution only.

    Returns:
        The enqueued job id, or ``None`` if an identical trigger already existed (the
        ``ON CONFLICT`` no-op).
    """
    payload = make_auto_topup_payload(
        old_balance=old_balance,
        new_balance=new_balance,
        source=source,
        call_id=call_id,
        turn_seq=turn_seq,
    )
    with engine.begin() as conn:
        row = conn.execute(
            _INSERT_SQL,
            {
                "type": AUTO_TOPUP_JOB_TYPE,
                "owner_id": owner_id,
                "payload": json.dumps(payload.model_dump()),
                "idempotency_key": auto_topup_idempotency_key(payload),
            },
        ).first()
    job_id = row[0] if row is not None else None
    _logger.info(
        "voice auto-top-up trigger enqueued (call={call} turn={turn} job={job})",
        call=call_id,
        turn=turn_seq,
        job=job_id or "<duplicate-noop>",
    )
    return job_id
