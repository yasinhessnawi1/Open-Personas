"""The auto-top-up job contract — the ONE definition both sides share (Spec M5, D-M5-15).

A voice call bills the caller straight to the DB through the core billing seam, with no
persona-api hop (D-M3-core-seam, deliberately, for latency). But an auto-top-up is a
**money action**: it needs Stripe credentials and the api's gateway, which persona-voice
must never hold. So voice does what A9 already established for every other cross-process
action it cannot perform itself: it **delegates**, by enqueuing a durable job the api
worker runs (D-M5-15).

That keeps Stripe credentials in one process, is off the audio loop by construction,
survives a voice-process death, and adds no new auth surface. The alternatives were
rejected in Phase 3: a service-to-service endpoint invents auth that does not exist and
loses durability; a Stripe call in a core seam spreads payment credentials into the voice
process; and hanging it off the exhaustion cutoff fires too late by definition, since the
cutoff happens at zero while a top-up must happen at the $2 crossing.

**The payload carries two BALANCES, never a decision** (D-M5-16). The threshold, the
Pro/opted-in gate and the active-subscription check all live in ``maybe_auto_topup`` on
the api side, so the voice process never learns what "$2" means and the rule cannot
desync across two processes.

As with :mod:`persona.jobs.delegation`, the ``jobs`` *table* stays api-owned; what
collapses to a single definition here is the SEMANTICS — payload shape, job-type string,
idempotency recipe — so the writer and the handler cannot disagree about what an
auto-top-up trigger is, only about how the row is INSERTed (pinned by a bidirectional
column-parity test plus a real-worker-transition test).
"""

from __future__ import annotations

from persona.jobs.models import JobPayload

__all__ = [
    "AUTO_TOPUP_JOB_TYPE",
    "AutoTopupPayload",
    "auto_topup_idempotency_key",
    "make_auto_topup_payload",
]

#: The durable job type string (the A0 ``jobs.type`` value + registry key).
AUTO_TOPUP_JOB_TYPE = "auto_topup_trigger"


class AutoTopupPayload(JobPayload):
    """One observed balance crossing, for the api side to evaluate (frozen, extra=forbid).

    Attributes:
        old_balance: The balance BEFORE the deduct that prompted this trigger.
        new_balance: The balance after it.
        source: The surface that observed the crossing (``voice``), for attribution in
            logs. It never affects the decision.
        call_id: The call the crossing happened on. Part of the idempotency identity, so
            one crossing on one call enqueues once no matter how many times the meter
            re-fires.
        turn_seq: The turn within that call, for the same reason.

    The payload deliberately carries NO threshold, NO amount and NO eligibility verdict:
    those are the api's to decide (D-M5-16). A payload that carried a decision would let
    a stale voice process authorise a charge under an outdated rule.
    """

    old_balance: int
    new_balance: int
    source: str
    call_id: str
    turn_seq: int


def make_auto_topup_payload(
    *, old_balance: int, new_balance: int, source: str, call_id: str, turn_seq: int
) -> AutoTopupPayload:
    """Build the payload both the writer and the handler agree on."""
    return AutoTopupPayload(
        old_balance=old_balance,
        new_balance=new_balance,
        source=source,
        call_id=call_id,
        turn_seq=turn_seq,
    )


def auto_topup_idempotency_key(payload: AutoTopupPayload) -> str:
    """``autotopup:{call_id}:{turn_seq}`` — one trigger per crossing per turn (D-M5-19).

    Mirrors the meter's own per-turn ``billing_key`` (``voice:{call_id}:{turn_seq}``), so
    the dedup identity of the TRIGGER matches the dedup identity of the DEDUCT that
    produced it. ``call_id`` is the per-call-unique session id, never the conversation id:
    a voice conversation spans several calls, so keying on it would collide triggers
    across calls (the same mistake that once caused a false mid-call cutoff).

    This is only the FIRST of four dedup layers. The DB's
    ``ON CONFLICT (owner_id, idempotency_key)`` is the real guarantee; the crossing guard
    in ``maybe_auto_topup`` cannot fire twice for one episode; and the hourly outbound
    Stripe key collapses concurrent charges to one PaymentIntent.
    """
    return f"autotopup:{payload.call_id}:{payload.turn_seq}"
