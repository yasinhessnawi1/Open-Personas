"""The injected Stripe gateway (Spec M4, T2a).

A thin wrapper over the Stripe v8+ ``StripeClient`` instance API (no module
globals — the client is constructed once and threaded via ``app.state``, mirroring
how ``rls_engine`` / ``credits_policy`` are injected). The ``stripe`` SDK is
**lazy-imported** inside :meth:`StripeGateway.__init__`, so a community / flag-off
boot (which never constructs a gateway — see
:func:`persona_api.editions.factory.build_stripe_gateway`) never imports the SDK.

T2a builds only the client + the config surface; the checkout / portal / webhook
methods land in T2b / T3.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import stripe

__all__ = ["StripeGateway"]


class StripeGateway:
    """Holds the Stripe ``StripeClient`` + the publishable / webhook config.

    Constructed ONLY on the active-billing path (cloud + flag + key), so importing
    ``stripe`` here can never fire on a community / flag-off boot. Constructing the
    client does no network I/O — it just binds the secret key — so this is safe at
    app-boot time.

    Args:
        secret_key: ``PERSONA_STRIPE_SECRET_KEY`` (test or live; server-side only).
        publishable_key: ``PERSONA_STRIPE_PUBLISHABLE_KEY`` (client-side; the web
            Stripe.js needs it — surfaced by ``GET /v1/billing/config``).
        webhook_secret: ``PERSONA_STRIPE_WEBHOOK_SECRET`` (used by the T3 webhook to
            verify the ``Stripe-Signature`` — held here so the whole billing config
            has one home).
    """

    def __init__(self, *, secret_key: str, publishable_key: str, webhook_secret: str) -> None:
        # Lazy import: the ONLY place the SDK is imported at runtime. The active-billing
        # gate upstream guarantees we never reach here on community / flag-off, so a
        # self-host install that never enables billing never imports stripe.
        import stripe  # noqa: PLC0415

        self._client: stripe.StripeClient = stripe.StripeClient(secret_key)
        self._publishable_key = publishable_key
        self._webhook_secret = webhook_secret

    @property
    def client(self) -> stripe.StripeClient:
        """The underlying Stripe client (the checkout / portal / webhook surface uses it)."""
        return self._client

    @property
    def publishable_key(self) -> str:
        """The client-side publishable key (surfaced to the web for Stripe.js)."""
        return self._publishable_key

    @property
    def webhook_secret(self) -> str:
        """The webhook signing secret (the T3 signature-verification input)."""
        return self._webhook_secret

    # --- T2b: checkout (subscribe) + customer portal + Stripe Tax --------------

    def create_customer(self, *, user_id: str, email: str | None) -> str:
        """Create a Stripe customer for ``user_id`` and return its id (Spec M4, T2b).

        Idempotent on ``customer:{user_id}`` (Stripe request idempotency) so two
        concurrent first-checkouts never mint duplicate customers — both get the SAME
        customer id. The caller persists it on ``subscription.stripe_customer_id`` and
        reuses it thereafter (create-or-fetch).
        """
        # ``email`` is omitted (not passed as None) when absent — Stripe wants it absent,
        # not null. The annotation is a string under ``from __future__ import annotations``
        # (no runtime ``stripe`` import — the SDK stays lazy); mypy reads it via the
        # TYPE_CHECKING import.
        params: stripe.params.CustomerCreateParams = {"metadata": {"user_id": user_id}}
        if email is not None:
            params["email"] = email
        customer = self._client.customers.create(
            params=params,
            options={"idempotency_key": f"customer:{user_id}"},
        )
        return customer.id

    def create_subscription_checkout(
        self, *, customer_id: str, price_id: str, success_url: str, cancel_url: str, user_id: str
    ) -> str:
        """A subscribe Checkout Session; return its hosted url (Spec M4, T2b/T3b).

        ``mode="subscription"`` with **Stripe Tax** on (``automatic_tax.enabled``) for
        EU/Norway VAT (D-M4-3). Subscription mode collects AND saves the payment method
        as the customer's default — off-session-chargeable, which is exactly what Pro
        auto-top-up (T7b) needs (the ``setup_future_usage:"off_session"`` intent; in
        subscription mode the save is automatic, so no such param is passed — it is a
        payment-mode param and Stripe would reject it here).

        ``metadata.user_id`` is stamped on both the session AND the subscription
        (``subscription_data.metadata``) — the T3b webhook tripwire cross-checks it against
        the DB customer→user resolution (a mismatch → refuse).
        """
        session = self._client.checkout.sessions.create(
            params={
                "mode": "subscription",
                "customer": customer_id,
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url,
                "cancel_url": cancel_url,
                "automatic_tax": {"enabled": True},
                "metadata": {"user_id": user_id},
                "subscription_data": {"metadata": {"user_id": user_id}},
            }
        )
        return session.url or ""

    def create_payg_checkout(
        self,
        *,
        customer_id: str,
        price_id: str,
        credit_amount: int,
        user_id: str,
        success_url: str,
        cancel_url: str,
    ) -> str:
        """A one-time PAYG dollar-pack Checkout Session; return its url (Spec M4, T4a).

        ``mode="payment"`` — where ``setup_future_usage:"off_session"`` is VALID (a
        payment-mode param): the pack purchase ALSO saves the card, belt-and-braces for Pro
        auto-top-up (T7b). **Stripe Tax** on. ``metadata.user_id`` + ``payg_credits`` are
        stamped on BOTH the session AND the ``payment_intent`` — the ``payment_intent.succeeded``
        handler reads ``payg_credits`` (the exact credits to grant, tax-free) + cross-checks
        ``user_id`` against the DB customer→user resolution (the tripwire).
        """
        meta = {"user_id": user_id, "payg_credits": str(credit_amount)}
        session = self._client.checkout.sessions.create(
            params={
                "mode": "payment",
                "customer": customer_id,
                "line_items": [{"price": price_id, "quantity": 1}],
                "success_url": success_url,
                "cancel_url": cancel_url,
                "automatic_tax": {"enabled": True},
                "metadata": meta,
                "payment_intent_data": {
                    "setup_future_usage": "off_session",  # save the card (Pro auto-top-up T7b)
                    "metadata": meta,
                },
            }
        )
        return session.url or ""

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        """A Stripe billing-portal session; return its url (Spec M4, T2b).

        The self-serve surface (plan change / cancel / payment-method update / invoice
        history); the portal-driven changes flow back as the T3 webhook lifecycle events.
        """
        session = self._client.billing_portal.sessions.create(
            params={"customer": customer_id, "return_url": return_url}
        )
        return session.url

    # --- T7b: Pro auto-top-up (off-session charge on the saved card) -----------

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        """Charge the customer's saved card OFF-SESSION and return ``(payment_intent_id, status)``.

        Spec M4 T7b: the Pro auto-top-up charge — ``payment_intents.create(off_session=True,
        confirm=True)`` bills the customer's default (saved) payment method WITHOUT the user
        present (the subscription checkout saved the card as the customer default, D-M4 T2b).
        ``amount`` is ``credit_amount`` cents (1 credit = 1¢, so ``$10`` ⇒ ``1000``); the grant
        is NOT done here — ``metadata.payg_credits`` rides to the existing
        ``payment_intent.succeeded`` webhook, which grants the PAYG lot idempotent on the PI id.

        ``idempotency_key`` (the hourly-bucketed ``autotopup:{user}:{YYYY-MM-DD-HH}``) makes a
        RETRIED trigger for the same low-balance crossing return the SAME PaymentIntent — one
        charge, never a double-charge. A card needing 3DS raises ``stripe.CardError`` with
        ``code == "authentication_required"`` (off-session can't complete the challenge); the
        caller catches it and falls back to an on-session top-up prompt (never a hard fail).
        """
        meta = {"user_id": user_id, "payg_credits": str(credit_amount), "source": "auto_topup"}
        intent = self._client.payment_intents.create(
            params={
                "amount": credit_amount,  # 1 credit = 1¢ → credits == cents (M3 accounting)
                "currency": "usd",
                "customer": customer_id,
                "off_session": True,
                "confirm": True,
                "metadata": meta,
            },
            options={"idempotency_key": idempotency_key},
        )
        return intent.id, intent.status or ""

    # --- T3a: webhook signature verification ----------------------------------

    def construct_event(self, *, payload: bytes, sig_header: str) -> stripe.Event:
        """Verify the ``Stripe-Signature`` + parse the event (Spec M4, T3a).

        ``payload`` MUST be the RAW request-body bytes (never re-serialised JSON — that
        breaks the HMAC). Raises ``stripe.SignatureVerificationError`` on a forged /
        stale signature and ``ValueError`` on a malformed payload; the caller
        (``billing.webhook.verify_and_dispatch``) normalises both to a
        ``WebhookVerificationError`` → 400 with zero side effect. The signature IS the
        auth for this unauthenticated server-to-server endpoint.
        """
        import stripe  # noqa: PLC0415 — active-billing path only (keeps the SDK lazy)

        # ``Webhook.construct_event`` is untyped in the SDK stubs (a legacy static
        # method) → the call is untyped + returns Any; cast back to the declared Event.
        event = stripe.Webhook.construct_event(  # type: ignore[no-untyped-call]
            payload, sig_header, self._webhook_secret
        )
        return cast("stripe.Event", event)
