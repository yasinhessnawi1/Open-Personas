"""The Stripe side of an account erasure (R9-284 T9, D-R9-284-account-erase).

The subscription is cancelled at once and every payment method detached; the Customer, its
email and its invoices stay for the legal retention period, with ``metadata.user_id`` replaced
by the erasure id. Every call is safe to repeat: a resumed erasure reads before it writes (an
ended subscription is not cancelled again, a detached method is no longer listed), each write
carries an idempotency key derived from the erasure id, and Stripe answering that the object is
already gone counts as done. The SDK stays lazily imported, as in :mod:`.gateway`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona.logging import get_logger

if TYPE_CHECKING:
    import stripe

__all__ = ["ERASURE_CANCEL_PARAMS", "StripeAccountEraser"]

_log = get_logger("api.erasure")

#: How an erased account's subscription is cancelled: at once, with no proration and no
#: refund, and no final invoice (D-R9-284-account-erase; the owner may change this).
ERASURE_CANCEL_PARAMS: stripe.params.SubscriptionCancelParams = {
    "prorate": False,
    "invoice_now": False,
}

# Subscription states that need no cancel.
_ENDED = frozenset({"canceled", "incomplete_expired"})
# Stripe error codes meaning the thing is already gone or already detached: done, not failed.
_ALREADY = frozenset({"resource_missing", "payment_method_unexpected_state"})


def _already_done(exc: Exception) -> bool:
    import stripe  # noqa: PLC0415 - lazy, as in gateway.py

    return isinstance(exc, stripe.InvalidRequestError) and exc.code in _ALREADY


def _key(erasure_id: str, action: str, object_id: str) -> stripe.RequestOptions:
    return {"idempotency_key": f"erase:{erasure_id}:{action}:{object_id}"}


class StripeAccountEraser:
    """Cancels, detaches and relabels one Customer's objects for an erasure."""

    def __init__(self, client: stripe.StripeClient | Any) -> None:  # noqa: ANN401
        self._client = client

    def erase_customer(self, customer_id: str, *, erasure_id: str) -> dict[str, int]:
        """Do all three for ``customer_id``; the counts of what this call changed."""
        return {
            "subscriptions_cancelled": self._cancel_subscriptions(customer_id, erasure_id),
            "payment_methods_detached": self._detach_payment_methods(customer_id, erasure_id),
            "customers_relabelled": self._relabel(customer_id, erasure_id),
        }

    def _cancel_subscriptions(self, customer_id: str, erasure_id: str) -> int:
        try:
            listed = self._client.subscriptions.list(
                params={"customer": customer_id, "status": "all", "limit": 100}
            )
            live = [s.id for s in listed.auto_paging_iter() if s.status not in _ENDED]
        except Exception as exc:
            if _already_done(exc):
                return 0
            raise
        cancelled = 0
        for subscription_id in live:
            try:
                self._client.subscriptions.cancel(
                    subscription_id,
                    params=ERASURE_CANCEL_PARAMS,
                    options=_key(erasure_id, "cancel", subscription_id),
                )
            except Exception as exc:
                if not _already_done(exc):
                    raise
                continue
            cancelled += 1
        return cancelled

    def _detach_payment_methods(self, customer_id: str, erasure_id: str) -> int:
        try:
            listed = self._client.customers.payment_methods.list(customer_id, params={"limit": 100})
            methods = [m.id for m in listed.auto_paging_iter()]
        except Exception as exc:
            if _already_done(exc):
                return 0
            raise
        detached = 0
        for method_id in methods:
            try:
                self._client.payment_methods.detach(
                    method_id, options=_key(erasure_id, "detach", method_id)
                )
            except Exception as exc:
                if not _already_done(exc):
                    raise
                continue
            detached += 1
        return detached

    def _relabel(self, customer_id: str, erasure_id: str) -> int:
        try:
            self._client.customers.update(
                customer_id,
                params={"metadata": {"user_id": erasure_id}},
                options=_key(erasure_id, "relabel", customer_id),
            )
        except Exception as exc:
            if _already_done(exc):
                _log.info("erasure: stripe customer already gone erasure={eid}", eid=erasure_id)
                return 0
            raise
        return 1
