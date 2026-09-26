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

from typing import TYPE_CHECKING, Any, cast

from persona.errors import PersonaError
from persona.logging import get_logger

if TYPE_CHECKING:
    import stripe

__all__ = [
    "AUTO_TOPUP_SOURCE",
    "AutoTopupPriceNotConfiguredError",
    "OffSessionAuthenticationRequiredError",
    "OffSessionChargeFailedError",
    "StripeGateway",
]

_log = get_logger("api.billing.gateway")

#: The ``metadata.source`` stamp on an auto top-up invoice AND its PaymentIntent. The
#: webhook handlers tell a top-up apart from a subscription renewal by this, never by shape.
AUTO_TOPUP_SOURCE = "auto_topup"

#: The two spellings Stripe uses for "the saved card needs the cardholder" (3DS/SCA):
#: ``authentication_required`` on a PaymentIntent confirm, and
#: ``invoice_payment_intent_requires_action`` on an invoice pay. One outcome for the caller.
_REQUIRES_ACTION_CODES = frozenset(
    {"authentication_required", "invoice_payment_intent_requires_action"}
)


def _is_resource_missing(exc: Exception) -> bool:
    """Whether Stripe answered that the object no longer exists (an already cleaned draft)."""
    return getattr(exc, "code", None) == "resource_missing" or (
        getattr(exc, "http_status", None) == 404
    )


class AutoTopupPriceNotConfiguredError(PersonaError):
    """No Stripe Price is configured for the pack the auto top-up bills.

    Raised BEFORE any Stripe call. ``PERSONA_STRIPE_PRICE_PACK_10`` is the same Price the
    Checkout pack path uses; without it there is no taxed line to bill, and inventing an
    ad-hoc amount is exactly what R9-177 B9 forbids.
    """


class OffSessionAuthenticationRequiredError(PersonaError):
    """The saved card needs the cardholder (3DS); the off-session charge cannot complete.

    The caller (``autotopup.maybe_auto_topup``) keys on ``code == "authentication_required"``
    and falls back to an on-session top-up prompt. The class attribute keeps that contract
    identical to the ``stripe.CardError`` the bare PaymentIntent used to raise, whichever
    of Stripe's two spellings the invoice pay came back with.
    """

    code = "authentication_required"


class OffSessionChargeFailedError(PersonaError):
    """The saved card was refused for a reason that is not 3DS (declined, expired, ...).

    ``context["code"]`` carries Stripe's decline code for the log; the caller treats this
    as a swallowed ``ERROR`` outcome, the next genuine crossing being the natural retry.
    """


def _invoice_payment_intent_id(invoice: Any) -> str | None:  # noqa: ANN401 (a StripeObject)
    """The id of the PaymentIntent a finalized ``charge_automatically`` invoice created.

    Reads the 2025+ shape ``invoice.payments.data[0].payment.payment_intent`` (an id, or
    the expanded object). ``None`` when Stripe has not surfaced one, which is a stamp we
    skip, never a charge we skip: the grant is anchored on the invoice, not the PI.
    """
    payments = getattr(invoice, "payments", None)
    data = getattr(payments, "data", None) or []
    for entry in data:
        payment = getattr(entry, "payment", None)
        pi = getattr(payment, "payment_intent", None)
        if isinstance(pi, str) and pi:
            return pi
        pi_id = getattr(pi, "id", None)
        if isinstance(pi_id, str) and pi_id:
            return pi_id
    return None


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
        autotopup_price_id: the Stripe Price the off-session auto top-up bills, which is
            the SAME ``PERSONA_STRIPE_PRICE_PACK_10`` the Checkout pack path uses (R9-177
            B9: one Price, one tax code, one truth). Empty means the auto top-up refuses
            before any Stripe call (:class:`AutoTopupPriceNotConfiguredError`).
    """

    def __init__(
        self,
        *,
        secret_key: str,
        publishable_key: str,
        webhook_secret: str,
        autotopup_price_id: str = "",
    ) -> None:
        # Lazy import: the ONLY place the SDK is imported at runtime. The active-billing
        # gate upstream guarantees we never reach here on community / flag-off, so a
        # self-host install that never enables billing never imports stripe.
        import stripe  # noqa: PLC0415

        self._client: stripe.StripeClient = stripe.StripeClient(secret_key)
        self._publishable_key = publishable_key
        self._webhook_secret = webhook_secret
        self._autotopup_price_id = autotopup_price_id

    @property
    def client(self) -> stripe.StripeClient:
        """The underlying Stripe client (the checkout / portal / webhook surface uses it)."""
        return self._client

    @property
    def autotopup_price_id(self) -> str:
        """The Price the auto top-up invoice bills (empty when the pack is unconfigured)."""
        return self._autotopup_price_id

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
                # R9-139: Stripe Tax needs an address, and a fresh Customer has none.
                # Collect it in Checkout and let Stripe save it back to the Customer;
                # without both, every first purchase failed with a 502.
                "billing_address_collection": "required",
                "customer_update": {"address": "auto", "name": "auto"},
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
                # R9-139: Stripe Tax needs an address, and a fresh Customer has none.
                # Collect it in Checkout and let Stripe save it back to the Customer;
                # without both, every first purchase failed with a 502.
                "billing_address_collection": "required",
                "customer_update": {"address": "auto", "name": "auto"},
                # A pack is always paid in USD: the webhook grants only a USD payment
                # (credits are US cents), so a buyer shown a localized NOK or EUR price
                # would pay and be refused. Adaptive Pricing can be switched on in the
                # dashboard, so this session opts out explicitly.
                "adaptive_pricing": {"enabled": False},
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

    # --- T7b: Pro auto-top-up (off-session TAXED invoice on the saved card) ----

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        """Bill the pack OFF-SESSION as a taxed Invoice; return ``(invoice_id, status)``.

        Spec M4 T7b, reshaped by R9-177 B9. The bare ``PaymentIntent`` this used to create
        could not take ``automatic_tax``, so the same pack bought by auto top-up was the
        only untaxed sale in the product. A Stripe Invoice can, and paying it off-session
        is Stripe's documented route for a taxed charge without the customer present:

        1. ``invoices.create`` with ``automatic_tax.enabled``, ``charge_automatically`` and
           ``auto_advance`` OFF (we finalize and pay in-line; Stripe never retries or
           voids on its own schedule);
        2. ``invoice_items.create`` for the pack's own Price (``autotopup_price_id``, the
           SAME object the Checkout pack path bills) attached to that invoice, never an
           ad-hoc amount, so the tax code and the price are one truth;
        3. ``finalize_invoice`` (computes tax, mints the PaymentIntent);
        4. stamp the same metadata on that PaymentIntent, so the T3b tripwire can
           cross-check ``user_id`` on either object;
        5. ``pay`` with ``off_session`` against the customer's default payment method.

        The returned id is the INVOICE id (it used to be a PaymentIntent id); ``status`` is
        the invoice's (``"paid"`` on success). The grant is NOT done here: the invoice
        carries ``metadata.payg_credits`` + ``source=auto_topup`` and the ``invoice.paid``
        webhook grants the PAYG lot idempotent on the invoice id. The invoice's own
        ``payment_intent.succeeded`` is left alone by that handler (same ``source`` stamp),
        so the credits land exactly once.

        ``idempotency_key`` (the hourly-bucketed ``autotopup:{user}:{YYYY-MM-DD-HH}``) roots
        every request in the chain (``:invoice`` / ``:item`` / ``:finalize`` / ``:pi-meta``
        / ``:pay``), so a RETRIED trigger for the same crossing replays the SAME invoice
        and Stripe never charges twice. A card needing 3DS raises
        :class:`OffSessionAuthenticationRequiredError` (``code == "authentication_required"``,
        the contract the caller already keys on) and any other refusal
        :class:`OffSessionChargeFailedError`; in both cases the invoice is voided so no
        open obligation lingers behind the on-session prompt. If ``finalize_invoice`` itself
        fails, the draft is deleted (its line first) and the finalize error is re-raised.

        Raises:
            AutoTopupPriceNotConfiguredError: no pack Price configured; nothing was sent.
            OffSessionAuthenticationRequiredError: the saved card needs the cardholder.
            OffSessionChargeFailedError: the saved card was refused for another reason.
        """
        import stripe  # noqa: PLC0415 (active-billing path only, keeps the SDK lazy)

        price_id = self._autotopup_price_id
        if not price_id:
            raise AutoTopupPriceNotConfiguredError(
                "auto top-up has no Stripe Price to bill; set PERSONA_STRIPE_PRICE_PACK_10",
                context={"user_id": user_id, "credit_amount": str(credit_amount)},
            )
        meta = {
            "user_id": user_id,
            "payg_credits": str(credit_amount),
            "source": AUTO_TOPUP_SOURCE,
        }

        invoice = self._client.invoices.create(
            params={
                "customer": customer_id,
                "collection_method": "charge_automatically",
                "auto_advance": False,
                "automatic_tax": {"enabled": True},  # the fix: the Checkout paths already do
                # Only the line we attach below may ride on this invoice; a pending item
                # from anywhere else must never be swept into an off-session charge.
                "pending_invoice_items_behavior": "exclude",
                "metadata": meta,
            },
            options={"idempotency_key": f"{idempotency_key}:invoice"},
        )
        item = self._client.invoice_items.create(
            params={
                "customer": customer_id,
                "invoice": invoice.id,
                "pricing": {"price": price_id},  # the pack's Price, tax code included
                "quantity": 1,
                "metadata": meta,
            },
            options={"idempotency_key": f"{idempotency_key}:item"},
        )
        try:
            finalized = self._client.invoices.finalize_invoice(
                invoice.id,
                params={"auto_advance": False},
                options={"idempotency_key": f"{idempotency_key}:finalize"},
            )
        except Exception:
            # R9-177 B9 edge (owner ruling 2026-09-26): a draft left behind by a failed
            # finalize (Stripe Tax cannot place the customer, say) would pile up in the
            # dashboard, one per crossing. Clean it up, then re-raise: the caller must
            # still see why the top-up failed.
            self._discard_draft_best_effort(invoice.id, item_id=item.id)
            raise
        self._stamp_invoice_payment_intent(finalized, meta=meta, idempotency_key=idempotency_key)

        try:
            paid = self._client.invoices.pay(
                invoice.id,
                params={"off_session": True},
                options={"idempotency_key": f"{idempotency_key}:pay"},
            )
        except stripe.CardError as exc:
            code = str(exc.code or "")
            self._void_best_effort(invoice.id, idempotency_key=idempotency_key)
            context = {"invoice_id": invoice.id, "user_id": user_id, "code": code}
            if code in _REQUIRES_ACTION_CODES:
                raise OffSessionAuthenticationRequiredError(
                    "the saved card needs the cardholder for this charge", context=context
                ) from exc
            raise OffSessionChargeFailedError(
                "the saved card was refused for the auto top-up", context=context
            ) from exc
        return paid.id, paid.status or ""

    def _stamp_invoice_payment_intent(
        self, invoice: stripe.Invoice, *, meta: dict[str, str], idempotency_key: str
    ) -> None:
        """Copy the top-up metadata onto the invoice's PaymentIntent (the T3b tripwire).

        Stripe does not copy invoice metadata to the PaymentIntent it mints, and the
        ``payment_intent.succeeded`` handler reads only the PI. Best-effort: a PI we
        cannot see, or a stamp Stripe refuses, is logged and the charge still proceeds,
        because the grant rides ``invoice.paid`` on the invoice's own metadata.
        """
        import stripe  # noqa: PLC0415 (active-billing path only, keeps the SDK lazy)

        pi_id = _invoice_payment_intent_id(invoice)
        if pi_id is None:
            _log.warning(
                "auto top-up invoice shows no payment intent after finalize; skipping the stamp",
                invoice_id=invoice.id,
            )
            return
        try:
            self._client.payment_intents.update(
                pi_id,
                params={"metadata": meta},
                options={"idempotency_key": f"{idempotency_key}:pi-meta"},
            )
        except stripe.StripeError:
            _log.opt(exception=True).warning(
                "auto top-up payment intent metadata stamp failed; the invoice still pays",
                invoice_id=invoice.id,
                payment_intent_id=pi_id,
            )

    def _discard_draft_best_effort(self, invoice_id: str, *, item_id: str) -> None:
        """Remove the pack's line, then delete the draft a failed finalize left behind.

        Only ever called when finalize raised, so the invoice is a draft. Stripe refuses
        to delete anything that is not a one-off draft, so a finalize that did complete
        behind a lost response cannot be deleted here either: the line removal fails
        first and the invoice is left alone.

        The line goes first because the invoice is deleted only once its line is gone. A
        line left behind by a deleted draft could become a pending item, and a later
        invoice for this customer that includes pending items would bill it. A draft that
        keeps its line is harmless, so when the line cannot be removed the draft stays.

        An hour-keyed replay can find the line, or the whole draft, already gone: the
        replayed create calls return the saved ids of what an earlier cleanup deleted.
        Stripe answers ``resource_missing`` for those, which means already cleaned, not kept.

        Best-effort: every failure here is logged and swallowed, so the caller always
        receives the finalize error. Logs name the invoice id only.
        """
        if not self._remove_draft_line(invoice_id, item_id=item_id):
            return
        try:
            self._client.invoices.delete(invoice_id)
        except Exception as exc:  # noqa: BLE001 (cleanup must never replace the finalize error)
            if _is_resource_missing(exc):
                _log.info(
                    "auto top-up draft invoice already cleaned up after a failed finalize",
                    invoice_id=invoice_id,
                )
                return
            self._unstamp_surviving_draft(invoice_id, delete_error=exc)
            return
        _log.info(
            "auto top-up draft invoice deleted after a failed finalize", invoice_id=invoice_id
        )

    def _remove_draft_line(self, invoice_id: str, *, item_id: str) -> bool:
        """Remove the pack's line from the draft; ``True`` when it is gone, now or before."""
        try:
            self._client.invoice_items.delete(item_id)
        except Exception as exc:  # noqa: BLE001 (cleanup must never replace the finalize error)
            if _is_resource_missing(exc):
                return True  # an earlier cleanup removed it; this is a replay
            _log.warning(
                "auto top-up draft invoice kept after a failed finalize: its line could not be "
                "removed",
                invoice_id=invoice_id,
                error=type(exc).__name__,
            )
            return False
        return True

    def _unstamp_surviving_draft(self, invoice_id: str, *, delete_error: Exception) -> None:
        """Clear the credit stamp on a lineless draft that could not be deleted (review M3).

        The draft keeps ``metadata.payg_credits``, and a same-hour replay can finalize it at
        $0, which Stripe marks paid. Clearing the stamp means it can never grant, whatever
        happens to it later; ``source`` stays so the webhooks still route it as a top-up.
        The webhook's own amount check is the second, independent guard.
        """
        try:
            self._client.invoices.update(invoice_id, params={"metadata": {"payg_credits": ""}})
        except Exception as exc:  # noqa: BLE001 (cleanup must never replace the finalize error)
            _log.warning(
                "auto top-up draft invoice could not be deleted after a failed finalize, and "
                "its credit stamp could not be cleared",
                invoice_id=invoice_id,
                error=type(exc).__name__,
            )
            return
        _log.warning(
            "auto top-up draft invoice could not be deleted after a failed finalize; its "
            "credit stamp was cleared so it can never grant",
            invoice_id=invoice_id,
            error=type(delete_error).__name__,
        )

    def _void_best_effort(self, invoice_id: str, *, idempotency_key: str) -> None:
        """Void a finalized invoice whose off-session payment was refused.

        Without this an open invoice would sit in the customer's portal next to the
        on-session prompt, two live routes to one intent. Best-effort: a void that fails
        is logged; the payment outcome the caller needs is raised regardless.
        """
        import stripe  # noqa: PLC0415 (active-billing path only, keeps the SDK lazy)

        try:
            self._client.invoices.void_invoice(
                invoice_id, options={"idempotency_key": f"{idempotency_key}:void"}
            )
        except stripe.StripeError:
            _log.opt(exception=True).warning(
                "auto top-up invoice could not be voided after a refused payment",
                invoice_id=invoice_id,
            )

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
