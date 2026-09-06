"""Billing routes (Spec M4, T2) — the Stripe surface, gated dark.

Every billing entrypoint runs through :func:`require_billing_gateway`: a community /
flag-off install has ``app.state.stripe_gateway is None`` → **404** (the surface
conceptually does not exist), never a partial response or a 500. T2a ships only the
read-only ``GET /v1/billing/config`` (the publishable key the web needs for Stripe.js);
T2b adds checkout + portal, T3 the webhook — all behind the same gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, status
from persona.billing import PAYG_PACKS, all_plans, get_payg_pack, payg_pack_code
from persona.logging import get_logger

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.billing import (
    StripeGateway,
    WebhookContext,
    WebhookVerificationError,
    verify_and_dispatch,
)
from persona_api.billing.autotopup import (
    AUTO_TOPUP_AMOUNT_CREDITS,
    AUTO_TOPUP_THRESHOLD_CREDITS,
)
from persona_api.errors import BillingProviderUnavailableError
from persona_api.schemas import (
    BillingConfigResponse,
    CheckoutRequest,
    CheckoutSessionResponse,
    PackCheckoutRequest,
    PaygPackOut,
    PlanOut,
    PortalSessionResponse,
)
from persona_api.services import subscription_service

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")

_log = get_logger("api.billing.routes")

router = APIRouter(prefix="/v1/billing", tags=["billing"])

__all__ = ["require_billing_gateway", "router"]


def require_billing_gateway(request: Request) -> StripeGateway:
    """Resolve the active Stripe gateway, or 404 when billing is disabled.

    The single dark-flag gate for every billing entrypoint (T2b checkout/portal +
    T3 webhook all depend on it). ``app.state.stripe_gateway`` is ``None`` on a
    community / flag-off boot (:func:`~persona_api.editions.factory.build_stripe_gateway`
    returns ``None``), so this 404s the whole surface there.
    """
    gateway: StripeGateway | None = getattr(request.app.state, "stripe_gateway", None)
    if gateway is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="billing is not enabled")
    return gateway


_STRIPE_UNAVAILABLE_DETAIL = "the payment provider is unavailable; nothing was charged"


def _stripe_call(what: str, call: Callable[[], _T]) -> _T:
    """Run one Stripe-backed call, converting a provider failure into a clean 502.

    Spec M5 (T3-fix). Every Stripe call in this module goes through here. Without it a
    Stripe outage (or a bad key) escapes as a **500 with a stack trace** — leaking
    internals and giving the web no structured error, so the purchase button spins
    forever instead of saying nothing was charged.

    Catch-at-the-boundary is deliberate (ENGINEERING_STANDARDS §1): the provider SDK's
    exceptions stop at the seam that owns the provider. ``502`` rather than a 4xx because
    the caller did nothing wrong — the dependency is down. No session is created when
    this fires, so there is no partial state and nothing to reconcile.
    """
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 — the provider seam: ANY SDK failure maps here
        _log.warning(
            "stripe call failed ({what}); returning 502: {err}", what=what, err=repr(exc)[:200]
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_STRIPE_UNAVAILABLE_DETAIL
        ) from BillingProviderUnavailableError(_STRIPE_UNAVAILABLE_DETAIL)


@router.get("/config", response_model=BillingConfigResponse)
async def get_billing_config(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — auth-gated read
) -> BillingConfigResponse:
    """The client-side billing config + the plan/pack catalog (Spec M4 T2a; M5 B3).

    404 when billing is disabled (community / flag-off). Never returns the secret key —
    only the client-safe publishable key.

    The catalog (D-M5-25) is projected straight from ``persona.billing.plans``, the
    owner-locked single source of truth, so the web renders prices and allowances it can
    never hardcode and can never drift from when the owner changes a number. It
    describes the OFFERING only — caller state lives on ``GET /v1/me/wallet``
    (D-M5-12), so the two responses cannot disagree about the same fact.
    """
    gateway = require_billing_gateway(request)
    return BillingConfigResponse(
        enabled=True,
        publishable_key=gateway.publishable_key,
        plans=[
            PlanOut(
                code=str(plan.code),
                monthly_price_credits=plan.monthly_price_credits,
                included_allowance_credits=plan.included_allowance_credits,
                auto_topup_eligible=plan.auto_topup_eligible,
                is_default=plan.is_default,
            )
            for plan in all_plans()
        ],
        # ``payg_pack_code`` is the inverse of the ``get_payg_pack`` lookup the pack
        # checkout route uses, so a rendered pack posts straight back to
        # ``/v1/billing/checkout/pack`` with no client-side code mapping to drift.
        packs=[
            PaygPackOut(
                code=payg_pack_code(pack),
                price_credits=pack.price_credits,
                granted_credits=pack.granted_credits,
                expiry_months=pack.expiry_months,
            )
            for pack in PAYG_PACKS
        ],
        auto_topup_threshold_credits=AUTO_TOPUP_THRESHOLD_CREDITS,
        auto_topup_amount_credits=AUTO_TOPUP_AMOUNT_CREDITS,
    )


@router.post("/checkout", response_model=CheckoutSessionResponse)
async def create_checkout(
    request: Request,
    body: CheckoutRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CheckoutSessionResponse:
    """Start a subscribe Checkout Session for the caller's own account (Spec M4, T2b).

    Resolves the plan's Stripe Price id from config (400 if the plan is not purchasable /
    the owner has not configured its Price id), reuses-or-creates the caller's ONE Stripe
    customer (stored on their own RLS-scoped ``subscription`` row), and returns the hosted
    checkout url with Stripe Tax on. 404 when billing is disabled; 401 unauthenticated.
    The session is bound to the caller's customer, so a user can never check out for
    another tenant.
    """
    gateway = require_billing_gateway(request)
    config = request.app.state.config
    price_id = config.stripe_price_id(body.plan_code)
    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"plan {body.plan_code!r} is not purchasable (no Stripe Price configured)",
        )
    customer_id = _stripe_call(
        "resolve_customer",
        lambda: subscription_service.resolve_stripe_customer(
            request.app.state.rls_engine, user_id=user.id, email=user.email, gateway=gateway
        ),
    )
    url = _stripe_call(
        "create_subscription_checkout",
        lambda: gateway.create_subscription_checkout(
            customer_id=customer_id,
            price_id=price_id,
            success_url=config.stripe_checkout_success_url,
            cancel_url=config.stripe_checkout_cancel_url,
            user_id=user.id,
        ),
    )
    return CheckoutSessionResponse(url=url)


@router.post("/checkout/pack", response_model=CheckoutSessionResponse)
async def create_pack_checkout(
    request: Request,
    body: PackCheckoutRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CheckoutSessionResponse:
    """Start a one-time PAYG dollar-pack Checkout Session for the caller (Spec M4, T4a).

    Resolves the pack's Price id from config (400 if the owner has not configured it) + its
    credit amount from the registry, reuses-or-creates the caller's ONE Stripe customer, and
    returns the hosted checkout url (Stripe Tax on; the card is saved for Pro auto-top-up).
    The grant happens on ``payment_intent.succeeded`` (T4a webhook handler). 404 when dark;
    401 unauthenticated; scoped to the caller.
    """
    gateway = require_billing_gateway(request)
    config = request.app.state.config
    pack = get_payg_pack(body.pack)
    price_id = config.stripe_price_for_pack(body.pack)
    if pack is None or not price_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"pack {body.pack!r} is not purchasable (no Stripe Price configured)",
        )
    customer_id = _stripe_call(
        "resolve_customer",
        lambda: subscription_service.resolve_stripe_customer(
            request.app.state.rls_engine, user_id=user.id, email=user.email, gateway=gateway
        ),
    )
    url = _stripe_call(
        "create_payg_checkout",
        lambda: gateway.create_payg_checkout(
            customer_id=customer_id,
            price_id=price_id,
            credit_amount=pack.granted_credits,
            user_id=user.id,
            success_url=config.stripe_checkout_success_url,
            cancel_url=config.stripe_checkout_cancel_url,
        ),
    )
    return CheckoutSessionResponse(url=url)


@router.post("/portal", response_model=PortalSessionResponse)
async def create_portal(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PortalSessionResponse:
    """Open the Stripe billing portal for the caller (Spec M4, T2b).

    Self-serve plan change / cancel / payment-method / invoices; the changes flow back
    as the T3 webhook lifecycle. Reuses the caller's own customer (RLS-scoped). 404 when
    disabled; 401 unauthenticated.
    """
    gateway = require_billing_gateway(request)
    config = request.app.state.config
    customer_id = _stripe_call(
        "resolve_customer",
        lambda: subscription_service.resolve_stripe_customer(
            request.app.state.rls_engine, user_id=user.id, email=user.email, gateway=gateway
        ),
    )
    url = _stripe_call(
        "create_portal_session",
        lambda: gateway.create_portal_session(
            customer_id=customer_id, return_url=config.stripe_portal_return_url
        ),
    )
    return PortalSessionResponse(url=url)


@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict[str, bool]:
    """The Stripe webhook — the payment system's security boundary (Spec M4, T3a).

    **UNAUTHENTICATED** (Stripe calls it server-to-server): the SIGNATURE is the only
    auth. Deliberately carries NO ``Depends(get_current_user)`` and NO Pydantic body
    model — a body model would auto-parse + re-encode the payload and break the HMAC.

    The RAW request body is read (``await request.body()``) BEFORE any parsing and
    verified against ``PERSONA_STRIPE_WEBHOOK_SECRET``. An absent header / forged or
    stale signature / malformed body → **400 with ZERO side effect** (rejected before
    any handler runs). A verified event dispatches to its handler (T3b; idempotent on
    ``event.id``); an unhandled type is a 200 no-op (never 4xx — that makes Stripe
    retry forever). 404 when billing is disabled (the dark gate).
    """
    gateway = require_billing_gateway(request)
    # RAW bytes, before any JSON parsing (the re-serialisation footgun).
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    context = WebhookContext(
        rls_engine=request.app.state.rls_engine,
        # admin_engine is used ONLY for the read-only customer→user resolve (never a write).
        admin_engine=request.app.state.admin_engine,
        credits_policy=request.app.state.credits_policy,
        config=request.app.state.config,
    )
    try:
        verify_and_dispatch(
            gateway=gateway,
            dispatcher=request.app.state.webhook_dispatcher,
            payload=payload,
            sig_header=sig_header,
            context=context,
        )
    except WebhookVerificationError as exc:
        # Bare 400 — never echo the reason/payload to an unauthenticated caller.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid webhook signature"
        ) from exc
    return {"received": True}
