"""Stripe subscription-lifecycle handlers (Spec M4, T3b).

Each handler applies one VERIFIED event (T3a already checked the signature). The
security invariant (Option A): the target user is resolved by a single READ-ONLY
``stripe_customer_id → user_id`` lookup on ``admin_engine``; then ``current_user_id``
is set to that user and EVERY write runs on ``rls_engine`` (RLS-scoped) — so a handler
bug can only ever touch the resolved user's rows. A ``metadata.user_id`` tripwire (set
by us at checkout) must match the DB resolution when present; an unknown customer is a
safe no-op (200, zero write).

The load-bearing money handler is ``invoice.paid``: it RESETS the allowance to the
plan's amount via :meth:`CreditsPolicy.reset_allowance_idempotent`, keyed on the invoice
id, so a re-delivery resets exactly once (never re-inflating a spent-down balance).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from persona.billing import get_plan
from persona.logging import get_logger

from persona_api.middleware.rls_context import current_user_id
from persona_api.services import subscription_service

if TYPE_CHECKING:
    from collections.abc import Callable

    import stripe

    from persona_api.billing.events import WebhookContext

__all__ = ["lifecycle_handlers"]

_log = get_logger("api.billing.handlers")


# --- Stripe object field extraction ------------------------------------------
#
# ``event.data.object`` is a ``stripe.StripeObject`` — NOT a dict/Mapping (no ``.get``),
# but it supports SUBSCRIPT access (``obj["k"]`` → value, ``KeyError`` if missing) and
# list indexing. ``_field`` navigates a path safely (any missing/None hop → ``None``).


def _obj(event: stripe.Event) -> Any:  # noqa: ANN401 — a StripeObject (dynamic)
    """The event's ``data.object`` (a StripeObject; access via :func:`_field`)."""
    return event.data.object


def _field(obj: Any, *path: str | int) -> Any:  # noqa: ANN401 — StripeObject navigation
    """Safely navigate ``obj[path[0]][path[1]]…`` (StripeObject subscript / list index).

    Any missing key, ``None`` hop, or out-of-range index → ``None`` (never raises).
    """
    node = obj
    for key in path:
        if node is None:
            return None
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError):
            return None
    return node


def _nested_price_id(container: Any) -> str | None:  # noqa: ANN401
    """The first line/item's Price id from an invoice or subscription object.

    Navigates ``<container>.<lines|items>.data[0].price.id``. Resolving the plan from the
    INVOICE LINE price (rather than the stored subscription row) is robust vs event ordering.
    """
    for collection in ("lines", "items"):
        price_id = _field(container, collection, "data", 0, "price", "id")
        if price_id:
            return str(price_id)
    return None


def _metadata_user_id(container: Any) -> str | None:  # noqa: ANN401
    """The ``user_id`` we stamped at checkout, from the object's metadata (the tripwire).

    Checks ``metadata.user_id`` (session / subscription) then
    ``subscription_details.metadata.user_id`` (invoice). Absent → ``None`` (DB resolution stands).
    """
    for path in (("metadata", "user_id"), ("subscription_details", "metadata", "user_id")):
        uid = _field(container, *path)
        if uid:
            return str(uid)
    return None


def _epoch_to_dt(epoch: object) -> datetime | None:
    """A Stripe unix-epoch field → an aware UTC datetime (``None`` if absent)."""
    if not epoch:
        return None
    return datetime.fromtimestamp(int(cast("int", epoch)), tz=UTC)


def _utc_month() -> str:
    """The current UTC calendar month ``'YYYY-MM'`` — matches T6's DB ``to_char`` marker."""
    return datetime.now(UTC).strftime("%Y-%m")


# --- resolve + scope + tripwire (the security core) ---------------------------


def _apply_scoped(context: WebhookContext, container: Any, write_fn: Callable[[str], None]) -> None:  # noqa: ANN401
    """Resolve the customer's user, verify the tripwire, then run ``write_fn`` RLS-scoped.

    The admin engine does ONLY the read-only resolve; ``write_fn`` runs with
    ``current_user_id`` set to the resolved user, so its writes (on ``context.rls_engine``)
    are RLS-enforced. Unknown customer → no-op; ``metadata.user_id`` mismatch → refuse.
    """
    customer = _field(container, "customer")
    if not customer:
        return  # no customer on the event → nothing to scope
    customer_id = str(customer)
    user_id = subscription_service.resolve_user_by_customer(
        context.admin_engine, stripe_customer_id=customer_id
    )
    if user_id is None:
        _log.info("stripe webhook: unknown customer — no-op", customer_id=customer_id)
        return  # safe no-op, zero write
    meta_uid = _metadata_user_id(container)
    if meta_uid is not None and meta_uid != user_id:
        # The customer→user binding (DB) and the metadata we stamped disagree — refuse.
        _log.warning(
            "stripe webhook: metadata.user_id mismatch — refusing",
            customer_id=customer_id,
            db_user=user_id,
            meta_user=meta_uid,
        )
        return  # refuse, zero write
    token = current_user_id.set(user_id)
    try:
        write_fn(user_id)
    finally:
        current_user_id.reset(token)


# --- the per-event handlers ---------------------------------------------------


def handle_checkout_completed(event: stripe.Event, context: WebhookContext) -> None:
    """``checkout.session.completed`` — bind the subscription id + mark active."""
    session = _obj(event)
    subscription_id = str(_field(session, "subscription") or "")

    def _write(user_id: str) -> None:
        subscription_service.bind_subscription(
            context.rls_engine,
            user_id=user_id,
            stripe_subscription_id=subscription_id,
            status="active",
        )

    _apply_scoped(context, session, _write)


def handle_subscription_updated(event: stripe.Event, context: WebhookContext) -> None:
    """``customer.subscription.updated`` — plan/status/period/cancel (monotonic-guarded)."""
    sub = _obj(event)
    plan_code = context.config.plan_code_for_price(_nested_price_id(sub) or "")
    period_start = _epoch_to_dt(_field(sub, "current_period_start"))
    period_end = _epoch_to_dt(_field(sub, "current_period_end"))
    if plan_code is None or period_start is None or period_end is None:
        return  # not one of our plans, or no period to guard on → no-op

    def _write(user_id: str) -> None:
        subscription_service.apply_subscription_update(
            context.rls_engine,
            user_id=user_id,
            plan_code=plan_code,
            status=str(_field(sub, "status") or ""),
            current_period_start=period_start,
            current_period_end=period_end,
            cancel_at_period_end=bool(_field(sub, "cancel_at_period_end")),
        )

    _apply_scoped(context, sub, _write)


def handle_subscription_deleted(event: stripe.Event, context: WebhookContext) -> None:
    """``customer.subscription.deleted`` — drop to Free, clear the sub id (NO grant)."""
    sub = _obj(event)
    _apply_scoped(
        context,
        sub,
        lambda user_id: subscription_service.mark_canceled(context.rls_engine, user_id=user_id),
    )


def handle_invoice_paid(event: stripe.Event, context: WebhookContext) -> None:
    """``invoice.paid`` — RESET the allowance to the plan amount, exactly once per invoice.

    The plan is resolved from the invoice LINE's Price id (robust vs event ordering); the
    reset is idempotent on the invoice id (a re-delivery does not re-inflate a spent-down
    balance). PAYG lots are untouched.
    """
    invoice = _obj(event)
    plan_code = context.config.plan_code_for_price(_nested_price_id(invoice) or "")
    invoice_id = str(_field(invoice, "id") or "")
    if plan_code is None or not invoice_id:
        return  # not our plan, or no invoice id to key on → no-op
    allowance = get_plan(plan_code).included_allowance_credits
    period = _utc_month()

    def _write(user_id: str) -> None:
        context.credits_policy.reset_allowance_idempotent(
            rls_engine=context.rls_engine,
            user_id=user_id,
            allowance=allowance,
            allowance_period=period,
            reason="subscription_renewal",
            billing_key=invoice_id,
            cost_basis="grant_subscription",
        )

    _apply_scoped(context, invoice, _write)


def handle_invoice_payment_failed(event: stripe.Event, context: WebhookContext) -> None:
    """``invoice.payment_failed`` — flip the subscription to ``past_due`` (no grant)."""
    invoice = _obj(event)
    _apply_scoped(
        context,
        invoice,
        lambda user_id: subscription_service.mark_past_due(context.rls_engine, user_id=user_id),
    )


def handle_payment_intent_succeeded(event: stripe.Event, context: WebhookContext) -> None:
    """``payment_intent.succeeded`` — grant a PAYG lot, exactly once per payment (Spec M4, T4a).

    A one-time pack purchase. The exact credit amount (tax-free) is read from
    ``payment_intent.metadata.payg_credits`` (stamped at checkout), NOT the charged amount
    (which includes tax). The lot is idempotent on the payment_intent id — a re-delivery
    grants ONE lot. Only fires for our PAYG intents (those carrying ``payg_credits``); a
    subscription invoice's own PI has no such metadata → no-op.
    """
    pi = _obj(event)
    pi_id = str(_field(pi, "id") or "")
    credits_raw = _field(pi, "metadata", "payg_credits")
    if not pi_id or not credits_raw:
        return  # not a PAYG pack purchase we stamped → no-op
    try:
        credit_amount = int(credits_raw)
    except (TypeError, ValueError):
        return
    if credit_amount <= 0:
        return

    def _write(user_id: str) -> None:
        context.credits_policy.grant_payg_lot_idempotent(
            rls_engine=context.rls_engine,
            user_id=user_id,
            credit_amount=credit_amount,
            reason="payg_topup",
            source_billing_key=pi_id,
            cost_basis="topup_payg",
        )

    _apply_scoped(context, pi, _write)


def lifecycle_handlers() -> dict[str, Callable[[stripe.Event, WebhookContext], None]]:
    """The subscription-lifecycle + PAYG handler registry (Spec M4, T3b/T4a)."""
    return {
        "checkout.session.completed": handle_checkout_completed,
        "customer.subscription.updated": handle_subscription_updated,
        "customer.subscription.deleted": handle_subscription_deleted,
        "invoice.paid": handle_invoice_paid,
        "invoice.payment_failed": handle_invoice_payment_failed,
        "payment_intent.succeeded": handle_payment_intent_succeeded,
    }
