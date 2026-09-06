"""Pro opt-in auto-top-up — off-session charge on a low-balance crossing (Spec M4, T7b).

When a **Pro, opted-in** user's balance CROSSES below $2 (200 credits) on a background
deduct, fire ONE off-session $10 charge on the saved card. The charge is created here; the
GRANT is NOT — the off-session PaymentIntent carries ``metadata.payg_credits`` and the
existing ``payment_intent.succeeded`` webhook (T4a) grants the PAYG lot idempotent on the
PI id. So a retried/re-delivered trigger for the same crossing (same hourly idempotency
key ⇒ same PI) never double-charges, and a re-delivered webhook never double-grants.

Safety rails (all enforced BEFORE any Stripe call):
- **crossing only** — ``old_balance >= 200 and new_balance < 200`` (fires once per
  low-balance episode, not on every sub-$2 deduct);
- **Pro + opted-in only** — ``plan.auto_topup_eligible`` AND ``subscription.auto_topup_enabled``
  AND an active status; a non-Pro or opted-out user NEVER reaches Stripe;
- **off the hot path** — the caller runs this via ``asyncio.to_thread`` from the background
  billing worker, so the turn/loop never blocks on the Stripe round-trip;
- **never hard-fails** — a card needing 3DS (``authentication_required``) ⇒ ``REQUIRES_ACTION``
  (the web surfaces an on-session top-up prompt); any other Stripe/network error is logged
  and swallowed (``ERROR``) so a billing hiccup can never break a completed turn.

Community / flag-off boots pass ``gateway=None`` ⇒ ``DISABLED`` (no plans, no Stripe).
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger
from persona.billing.plans import PlanCode, get_plan

from persona_api.services import subscription_service

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Engine

    from persona_api.billing import StripeGateway

__all__ = [
    "AUTO_TOPUP_AMOUNT_CREDITS",
    "AUTO_TOPUP_THRESHOLD_CREDITS",
    "AutoTopupOutcome",
    "maybe_auto_topup",
]

# Owner-locked Phase-4 numbers (D-M4-6): trigger below $2, top up $10 (1 credit = 1¢).
AUTO_TOPUP_THRESHOLD_CREDITS = 200  # $2 — the low-balance crossing threshold
AUTO_TOPUP_AMOUNT_CREDITS = 1000  # $10 — the off-session top-up charge (== the granted lot)


class AutoTopupOutcome(StrEnum):
    """The result of a trigger evaluation (for the caller's logging + the tests' assertions)."""

    NOT_CROSSED = "not_crossed"  # the deduct did not cross the $2 threshold — no-op
    NOT_ELIGIBLE = "not_eligible"  # non-Pro, opted-out, or inactive — NEVER touches Stripe
    DISABLED = "disabled"  # community / flag-off (no gateway) — no plans, no Stripe
    NO_CUSTOMER = "no_customer"  # Pro + opted-in but no saved card — on-session prompt instead
    CHARGED = "charged"  # ONE off-session charge fired; the webhook grants the lot
    REQUIRES_ACTION = "requires_action"  # 3DS needed — fall back to an on-session top-up prompt
    ERROR = "error"  # a Stripe/network error, logged + swallowed (the turn is never broken)


def _hourly_idempotency_key(user_id: str, now: datetime) -> str:
    """``autotopup:{user}:{YYYY-MM-DD-HH}`` (UTC) — a retried trigger for the SAME crossing
    reuses the SAME PaymentIntent ⇒ exactly one charge. A rare same-hour 2nd crossing is
    blocked gracefully (Stripe returns the first PI; the balance is already topped up)."""
    return f"autotopup:{user_id}:{now:%Y-%m-%d-%H}"


#: The outcomes a USER must hear about, and the notification each maps to (Spec M5, B2).
#:
#: Only the two that need a human. ``CHARGED`` is silent by design: the credits simply
#: appear, and a notification for every successful top-up would train people to ignore
#: the bell before the one that matters arrives. ``NOT_CROSSED`` / ``NOT_ELIGIBLE`` /
#: ``DISABLED`` are non-events. ``ERROR`` is a transient provider failure the next
#: crossing retries, so it stays in the log rather than alarming the user about a
#: hiccup they cannot act on.
_NOTIFIED_OUTCOMES: dict[str, tuple[str, str]] = {
    # (level, message_key). REQUIRES_ACTION is the load-bearing one: the charge genuinely
    # needs the cardholder, and silence here means a top-up that never completes and a
    # balance that runs out with no explanation.
    "requires_action": ("warning", "notifications.autoTopup.requiresAction"),
    "no_customer": ("warning", "notifications.autoTopup.noCustomer"),
}


def _notify_outcome(
    rls_engine: Engine, *, user_id: str, outcome: AutoTopupOutcome, now: datetime
) -> None:
    """Tell the user when an auto-top-up needs them (Spec M5, B2 — D-M5-26).

    Durable notifications, not the live event stream: this fires from a detached
    background thread after a turn, when the user may have no stream open. A dropped
    message here is exactly the user whose card silently stopped working.

    Idempotent per user per hour via the ``ref_id`` — the same hour the outbound Stripe
    key uses (:func:`_hourly_idempotency_key`), so a retried trigger for one crossing
    produces ONE bell entry, not one per attempt.

    Best-effort: a feed-write failure must never break the caller's completed turn, which
    is the same discipline every other server-authored notification write follows.
    """
    mapped = _NOTIFIED_OUTCOMES.get(str(outcome))
    if mapped is None:
        return
    level, message_key = mapped
    try:
        from persona_api.middleware.rls_context import current_user_id  # noqa: PLC0415
        from persona_api.services import notifications_service  # noqa: PLC0415

        token = current_user_id.set(user_id)
        try:
            with rls_engine.begin() as conn:
                notifications_service.create_notification(
                    conn=conn,
                    owner_id=user_id,
                    kind="auto_topup",
                    ref_id=_hourly_idempotency_key(user_id, now),
                    level=level,
                    message_key=message_key,
                    params={},
                )
        finally:
            current_user_id.reset(token)
    except Exception:  # noqa: BLE001 — a bell write must never break a completed turn
        logger.opt(exception=True).warning(
            "auto-top-up outcome notification failed for user {}", user_id
        )


def maybe_auto_topup(
    *,
    rls_engine: Engine,
    gateway: StripeGateway | None,
    user_id: str,
    old_balance: int,
    new_balance: int,
    now: datetime | None = None,
) -> AutoTopupOutcome:
    """Evaluate + (if warranted) fire a Pro auto-top-up for ``user_id`` (Spec M4, T7b).

    Runs OFF the hot path (the caller offloads via ``asyncio.to_thread``). Returns an
    :class:`AutoTopupOutcome` describing what happened; never raises (a completed turn must
    never be broken by a billing side effect).
    """
    from datetime import UTC  # noqa: PLC0415 — local, keeps import surface tight
    from datetime import datetime as _dt

    now = now or _dt.now(UTC)

    # 1. Crossing guard — fire ONCE per low-balance episode, not on every sub-$2 deduct.
    if not (old_balance >= AUTO_TOPUP_THRESHOLD_CREDITS > new_balance):
        return AutoTopupOutcome.NOT_CROSSED

    # 2. Community / flag-off — no plans, no Stripe (edition-gated).
    if gateway is None:
        return AutoTopupOutcome.DISABLED

    # 3. Eligibility — Pro plan that OFFERS auto-top-up, opted IN, and an active subscription.
    #    A non-Pro or opted-out user NEVER reaches Stripe (asserted adversarially).
    sub = subscription_service.get_subscription(rls_engine, user_id=user_id)
    if sub is None or not _is_auto_topup_eligible(sub):
        return AutoTopupOutcome.NOT_ELIGIBLE

    customer_id = sub.get("stripe_customer_id")
    if not customer_id or not isinstance(customer_id, str):
        # Opted in but no saved card/customer — can't charge off-session; prompt on-session.
        # B2: the user asked for this and it cannot happen, so they are told (D-M5-26).
        _notify_outcome(rls_engine, user_id=user_id, outcome=AutoTopupOutcome.NO_CUSTOMER, now=now)
        return AutoTopupOutcome.NO_CUSTOMER

    # 4. The off-session charge (idempotent per hourly crossing). The GRANT is the webhook's.
    try:
        pi_id, status = gateway.create_off_session_topup(
            customer_id=customer_id,
            credit_amount=AUTO_TOPUP_AMOUNT_CREDITS,
            user_id=user_id,
            idempotency_key=_hourly_idempotency_key(user_id, now),
        )
    except Exception as exc:  # noqa: BLE001 — a billing side effect must NEVER break the turn
        if getattr(exc, "code", None) == "authentication_required":
            # 3DS: off-session can't complete the challenge → on-session top-up prompt.
            # B2: THE outcome that most needs a human. Without this the charge never
            # completes and the balance runs out with no explanation (D-M5-26).
            logger.info("auto-top-up needs 3DS for user {}; falling back on-session", user_id)
            _notify_outcome(
                rls_engine, user_id=user_id, outcome=AutoTopupOutcome.REQUIRES_ACTION, now=now
            )
            return AutoTopupOutcome.REQUIRES_ACTION
        logger.opt(exception=True).warning("auto-top-up charge failed for user {}", user_id)
        return AutoTopupOutcome.ERROR

    logger.info(
        "auto-top-up charged user {} (pi={} status={}); grant rides payment_intent.succeeded",
        user_id,
        pi_id,
        status,
    )
    return AutoTopupOutcome.CHARGED


def _is_auto_topup_eligible(sub: object) -> bool:
    """True iff the subscription row is a Pro plan that offers auto-top-up, opted IN, active."""
    if not isinstance(sub, dict):
        return False
    if not bool(sub.get("auto_topup_enabled")):
        return False
    if str(sub.get("status") or "active") != "active":
        return False
    plan_code = str(sub.get("plan_code") or PlanCode.free)
    try:
        plan = get_plan(plan_code)
    except (KeyError, ValueError):
        return False
    return plan.auto_topup_eligible
