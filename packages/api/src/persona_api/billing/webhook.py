"""Webhook verification + dispatch (Spec M4, T3a) — the payment-system security boundary.

The ONLY thing between an inbound request and any side effect is the Stripe signature.
:func:`verify_and_dispatch` verifies the ``Stripe-Signature`` over the RAW body, then
routes the parsed event; ANY failure (absent header / forged or stale signature /
malformed payload) raises :class:`WebhookVerificationError` BEFORE dispatch, so the
route answers 400 with ZERO side effect (no ledger, no subscription write). Verified
events dispatch to the T3b handlers (idempotent on ``event.id``); an unhandled type is
a 200 no-op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.logging import get_logger

if TYPE_CHECKING:
    from persona_api.billing.events import EventDispatcher, WebhookContext
    from persona_api.billing.gateway import StripeGateway

__all__ = ["WebhookVerificationError", "verify_and_dispatch"]

_log = get_logger("api.billing.webhook")


class WebhookVerificationError(Exception):
    """Signature verification or payload parsing failed → 400, zero side effect.

    Raised for an absent ``Stripe-Signature`` header, a forged/stale signature, or a
    malformed body. Never carries the raw payload (avoid logging attacker-controlled
    bytes); the route maps it to a bare 400.
    """


def verify_and_dispatch(
    *,
    gateway: StripeGateway,
    dispatcher: EventDispatcher,
    payload: bytes,
    sig_header: str | None,
    context: WebhookContext,
) -> str:
    """Verify the signature over ``payload``, parse, and dispatch. Return the event type.

    ``payload`` is the RAW request-body bytes. Raises :class:`WebhookVerificationError`
    (→ 400, zero side effect) when the header is absent, the signature is forged/stale,
    or the body is malformed — the check runs BEFORE any handler, so a rejected request
    can never write. On success the event is dispatched (a handled type applies its
    idempotent effect; an unhandled type is a no-op) and the type is returned.
    """
    # Lazy import: only reached on the active-billing path (the route is gate-guarded),
    # so ``stripe`` stays unimported for community / flag-off.
    import stripe  # noqa: PLC0415

    if not sig_header:
        raise WebhookVerificationError("missing Stripe-Signature header")
    try:
        event = gateway.construct_event(payload=payload, sig_header=sig_header)
    except (stripe.SignatureVerificationError, ValueError) as exc:
        # Both the forged-signature and malformed-payload paths land here — normalise to
        # the domain error. type(exc).__name__ only (never the payload/message details).
        _log.warning("stripe webhook verification failed", error_class=type(exc).__name__)
        raise WebhookVerificationError(type(exc).__name__) from exc
    dispatcher.dispatch(event, context)
    return str(event.type)
