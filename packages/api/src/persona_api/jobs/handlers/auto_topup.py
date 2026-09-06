"""The api-side auto-top-up handler — where the money decision actually happens (Spec M5, B5).

Voice observes a balance crossing and cannot act on it: an auto-top-up needs Stripe
credentials the voice process must never hold (D-M5-15). So voice enqueues a durable
``auto_topup_trigger`` job carrying two balances, and this handler — running in the api
worker, which does hold the gateway — evaluates it.

**The decision lives here, not in the payload** (D-M5-16). The handler passes the reported
balances straight to :func:`maybe_auto_topup`, whose own guards decide everything: whether
those balances constitute a crossing of the $2 line, whether the caller is a Pro,
opted-in, active subscriber, and whether a charge fires. The voice process never learns
what "$2" means, so the rule cannot desync across two processes, and a stale voice build
can never authorise a charge under an outdated threshold.

Registered ONLY when the Stripe gateway is active (see
``worker_root._register_auto_topup_tenant``): community and flag-off boots do not register
the tenant at all, so the ``stripe`` SDK is never imported and the registry is
byte-identical to a pre-M5 worker (D-M5-24).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.jobs import (
    AUTO_TOPUP_JOB_TYPE,
    SHORT_LEASE,
    AutoTopupPayload,
    JobTypeSpec,
    RetryPolicy,
    auto_topup_idempotency_key,
)
from persona.logging import get_logger

from persona_api.billing.autotopup import maybe_auto_topup

if TYPE_CHECKING:
    from persona.jobs import JobContext, JobRegistry
    from sqlalchemy import Engine

    from persona_api.billing import StripeGateway

__all__ = ["AutoTopupHandler", "register_auto_topup_handler"]

_log = get_logger("api.jobs.auto_topup")


class AutoTopupHandler:
    """Evaluates one reported balance crossing, and charges if warranted.

    Idempotent by construction, at three layers beyond the queue's own ``ON CONFLICT``:
    the crossing guard cannot fire twice for one low-balance episode, the hourly outbound
    Stripe key collapses concurrent charges to a single PaymentIntent, and the grant
    itself rides ``payment_intent.succeeded`` keyed on that PI id. So a re-delivered or
    retried job cannot produce a second charge.
    """

    def __init__(self, *, rls_engine: Engine, gateway: StripeGateway) -> None:
        self._engine = rls_engine
        self._gateway = gateway

    async def handle(self, payload: AutoTopupPayload, context: JobContext) -> None:
        """Run the trigger for the job's owner (never the payload's — RLS scope is the job's)."""
        outcome = maybe_auto_topup(
            rls_engine=self._engine,
            gateway=self._gateway,
            user_id=context.owner_id,
            old_balance=payload.old_balance,
            new_balance=payload.new_balance,
        )
        _log.info(
            "auto-top-up trigger from {source} evaluated: {outcome} (call={call} turn={turn})",
            source=payload.source,
            outcome=str(outcome),
            call=payload.call_id,
            turn=payload.turn_seq,
        )


def register_auto_topup_handler(
    registry: JobRegistry, *, rls_engine: Engine, gateway: StripeGateway
) -> None:
    """Register the auto-top-up tenant (the title-refresh registration shape).

    ``max_attempts=1``: a retry cannot help. Every terminal outcome is either already
    final (charged, not eligible, not a crossing) or needs a human (3DS, no saved card,
    both surfaced through the T4b notification). Retrying a declined card would hammer it
    to no purpose, and the NEXT genuine crossing is the natural retry.
    """
    registry.register(
        JobTypeSpec(
            type=AUTO_TOPUP_JOB_TYPE,
            payload_model=AutoTopupPayload,
            handler=AutoTopupHandler(rls_engine=rls_engine, gateway=gateway),
            idempotency_key=auto_topup_idempotency_key,
            retry=RetryPolicy(max_attempts=1),
            lease=SHORT_LEASE,
        )
    )
