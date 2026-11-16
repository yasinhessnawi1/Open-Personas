"""The Stripe side of an account erasure (R9-284 T9, D-R9-284-account-erase).

The subscription is cancelled at once and every payment method detached; the Customer, its
email and its invoices stay for the legal retention period, with ``metadata.user_id`` replaced
by the erasure id. Every call is safe to repeat: a resumed erasure reads before it writes (an
ended subscription is not cancelled again, a detached method is no longer listed), each write
carries an idempotency key derived from the erasure id, and Stripe answering that the object is
already gone counts as done. The SDK stays lazily imported, as in :mod:`.gateway`.

Only the Customer is relabelled (review L1). The other Stripe objects we stamped at checkout
(the Subscription, Checkout Sessions, Invoices, PaymentIntents) keep ``metadata.user_id`` with
the old user id: they are kept records Stripe holds for the retention period anyway, rewriting
each would be one API call per object for every past invoice, and the id reveals nothing the
retention does not already keep, since our ``erasures`` ledger maps the erasure id to the user
id by design (D-R9-284-erase ruling 7). What matters, that no live user row resolves from them,
holds: the customer resolves to no user once the account is gone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona.logging import get_logger

if TYPE_CHECKING:
    import stripe

__all__ = ["ERASED_PREFIX", "ERASURE_CANCEL_PARAMS", "StripeAccountEraser", "cancel_if_erased"]

#: The erasure ids written into an erased Customer's ``metadata.user_id`` start with this.
ERASED_PREFIX = "erasure_"

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
            # First, so no open Checkout can still turn into a subscription (review M4).
            "checkout_sessions_expired": self._expire_checkout_sessions(customer_id, erasure_id),
            "subscriptions_cancelled": self._cancel_subscriptions(customer_id, erasure_id),
            "payment_methods_detached": self._detach_payment_methods(customer_id, erasure_id),
            "customers_relabelled": self._relabel(customer_id, erasure_id),
        }

    def _expire_checkout_sessions(self, customer_id: str, erasure_id: str) -> int:
        try:
            listed = self._client.checkout.sessions.list(
                params={"customer": customer_id, "status": "open", "limit": 100}
            )
            sessions = [s.id for s in listed.auto_paging_iter()]
        except Exception as exc:
            if _already_done(exc):
                return 0
            raise
        expired = 0
        for session_id in sessions:
            try:
                self._client.checkout.sessions.expire(
                    session_id, options=_key(erasure_id, "expire", session_id)
                )
            except Exception as exc:
                if not _already_done(exc):
                    raise
                continue
            expired += 1
        return expired

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


def cancel_if_erased(
    client: stripe.StripeClient | Any,  # noqa: ANN401
    *,
    customer_id: str,
    subscription_id: str,
) -> bool:
    """Cancel a subscription that appeared for an erased account's Customer (review M4).

    For a webhook about a customer we no longer know: the erasure relabelled the Customer's
    ``metadata.user_id`` with its erasure id, so a subscription created there afterwards (a
    Checkout completed in the instant before it was expired) is cancelled at once, with the
    same parameters as the erasure's own cancel. ``True`` when it cancelled one.
    """
    if not customer_id or not subscription_id:
        return False
    try:
        customer = client.customers.retrieve(customer_id)
    except Exception as exc:
        if _already_done(exc):
            return False
        raise
    metadata = getattr(customer, "metadata", None) or {}
    erasure_id = str(metadata.get("user_id") or "")
    if not erasure_id.startswith(ERASED_PREFIX):
        return False
    # Review L-2: read it first; an ended one (our own cancel's later events) is left alone.
    try:
        subscription = client.subscriptions.retrieve(subscription_id)
    except Exception as exc:
        if _already_done(exc):
            return False
        raise
    if getattr(subscription, "status", None) in _ENDED:
        return False
    try:
        client.subscriptions.cancel(
            subscription_id,
            params=ERASURE_CANCEL_PARAMS,
            options=_key(erasure_id, "cancel", subscription_id),
        )
    except Exception as exc:
        if not _already_done(exc):
            raise
        return False
    _log.warning(
        "erasure: cancelled a subscription made after the erasure erasure={eid}", eid=erasure_id
    )
    return True
