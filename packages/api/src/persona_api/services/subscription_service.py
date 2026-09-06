"""Subscription store + Stripe-customer reconciliation (Spec M4, T2b).

The per-user ``subscription`` row (one per user; default ``plan_code='free'`` — a user
with no paid sub is permanently Free, D-M4-9) + the create-or-fetch of the ONE Stripe
customer per user. RLS-scoped by ``user_id`` (the caller's tenant scope), so a
subscription/customer is only ever read/written under its owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.models import subscription as _sub_t

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from sqlalchemy import Engine

    from persona_api.billing import StripeGateway

__all__ = [
    "apply_subscription_update",
    "bind_subscription",
    "ensure_subscription",
    "get_subscription",
    "mark_canceled",
    "mark_past_due",
    "resolve_stripe_customer",
    "resolve_user_by_customer",
    "set_auto_topup",
    "set_stripe_customer",
]


def ensure_subscription(rls_engine: Engine, *, user_id: str) -> Mapping[str, object]:
    """Ensure the caller's subscription row exists (default Free); return it.

    Insert-if-absent (``ON CONFLICT DO NOTHING``) then read — mirrors
    ``credits.ensure_balance``. A fresh user gets a ``plan_code='free'`` row.
    """
    with rls_engine.begin() as conn:
        conn.execute(
            pg_insert(_sub_t)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=["user_id"])
        )
        row = conn.execute(select(_sub_t).where(_sub_t.c.user_id == user_id)).mappings().one()
    return dict(row)


def get_subscription(rls_engine: Engine, *, user_id: str) -> Mapping[str, object] | None:
    """The caller's subscription row, or ``None`` if it has never been provisioned."""
    with rls_engine.begin() as conn:
        row = conn.execute(select(_sub_t).where(_sub_t.c.user_id == user_id)).mappings().first()
    return dict(row) if row is not None else None


def set_stripe_customer(rls_engine: Engine, *, user_id: str, customer_id: str) -> str:
    """Store the Stripe customer id IF NOT ALREADY SET; return the effective id.

    Conditional (``WHERE stripe_customer_id IS NULL``) so a concurrent set never
    clobbers — the second writer's UPDATE matches no row and the already-stored id
    is returned. Combined with the ``create_customer`` idempotency key (same id for
    concurrent creates), this yields exactly one customer per user with no orphan.
    """
    with rls_engine.begin() as conn:
        conn.execute(
            update(_sub_t)
            .where(_sub_t.c.user_id == user_id, _sub_t.c.stripe_customer_id.is_(None))
            .values(stripe_customer_id=customer_id, updated_at=text("now()"))
        )
        stored = conn.execute(
            select(_sub_t.c.stripe_customer_id).where(_sub_t.c.user_id == user_id)
        ).scalar_one()
    return str(stored)


def resolve_stripe_customer(
    rls_engine: Engine, *, user_id: str, email: str | None, gateway: StripeGateway
) -> str:
    """The caller's Stripe customer id — reused if present, else created + stored.

    Ensures the subscription row, returns its ``stripe_customer_id`` when set, else
    creates the customer (idempotent on the user) and stores it. Exactly one customer
    per user (create-or-fetch), reused by every checkout / portal call.
    """
    row = ensure_subscription(rls_engine, user_id=user_id)
    existing = row.get("stripe_customer_id")
    if existing:
        return str(existing)
    customer_id = gateway.create_customer(user_id=user_id, email=email)
    return set_stripe_customer(rls_engine, user_id=user_id, customer_id=customer_id)


# --- T3b: webhook resolution (READ-ONLY, admin engine) + lifecycle writes (RLS engine) -----


def resolve_user_by_customer(admin_engine: Engine, *, stripe_customer_id: str) -> str | None:
    """Resolve which user owns a Stripe customer — a cross-tenant READ-ONLY lookup (Spec M4, T3b).

    The ONLY cross-tenant / admin-engine access in the webhook path: an exact-match SELECT on
    ``subscription`` (whose ``stripe_customer_id`` we populated at checkout under the
    authenticated user's OWN scope, so the binding is trustworthy — the event itself is
    signature-verified, so the customer id is real). NEVER a write. The caller then sets
    ``current_user_id`` to this result and does every WRITE on the RLS-scoped engine, so a
    handler bug can only touch the resolved user's rows. ``None`` for an unknown customer →
    the handler no-ops (200, zero write).
    """
    with admin_engine.begin() as conn:
        row = conn.execute(
            select(_sub_t.c.user_id).where(_sub_t.c.stripe_customer_id == stripe_customer_id)
        ).first()
    return str(row[0]) if row is not None else None


def bind_subscription(
    rls_engine: Engine, *, user_id: str, stripe_subscription_id: str, status: str
) -> None:
    """Bind the Stripe subscription id + status on the resolved user's row (checkout.completed)."""
    with rls_engine.begin() as conn:
        conn.execute(
            update(_sub_t)
            .where(_sub_t.c.user_id == user_id)
            .values(
                stripe_subscription_id=stripe_subscription_id,
                status=status,
                updated_at=text("now()"),
            )
        )


def apply_subscription_update(
    rls_engine: Engine,
    *,
    user_id: str,
    plan_code: str,
    status: str,
    current_period_start: datetime,
    current_period_end: datetime,
    cancel_at_period_end: bool,
) -> None:
    """Apply a ``customer.subscription.updated`` — plan/status/period/cancel (Spec M4, T3b).

    MONOTONIC guard: the UPDATE only applies when the event's ``current_period_start`` is
    not older than the stored one (``stored IS NULL OR stored <= :new_start``), so a stale
    out-of-order delivery cannot regress the row to an earlier period/state.
    """
    with rls_engine.begin() as conn:
        conn.execute(
            update(_sub_t)
            .where(
                _sub_t.c.user_id == user_id,
                (_sub_t.c.current_period_start.is_(None))
                | (_sub_t.c.current_period_start <= current_period_start),
            )
            .values(
                plan_code=plan_code,
                status=status,
                current_period_start=current_period_start,
                current_period_end=current_period_end,
                cancel_at_period_end=cancel_at_period_end,
                updated_at=text("now()"),
            )
        )


def mark_canceled(rls_engine: Engine, *, user_id: str) -> None:
    """``customer.subscription.deleted`` — drop to Free, clear the sub id, status canceled.

    NO ledger change: the allowance is left as-is until T6's monthly free reset overwrites it.
    """
    with rls_engine.begin() as conn:
        conn.execute(
            update(_sub_t)
            .where(_sub_t.c.user_id == user_id)
            .values(
                plan_code="free",
                status="canceled",
                stripe_subscription_id=None,
                cancel_at_period_end=False,
                updated_at=text("now()"),
            )
        )


def mark_past_due(rls_engine: Engine, *, user_id: str) -> None:
    """``invoice.payment_failed`` — flip the subscription to ``past_due`` (no grant)."""
    with rls_engine.begin() as conn:
        conn.execute(
            update(_sub_t)
            .where(_sub_t.c.user_id == user_id)
            .values(status="past_due", updated_at=text("now()"))
        )


def set_auto_topup(rls_engine: Engine, *, user_id: str, enabled: bool) -> bool:
    """Set the caller's auto-top-up opt-in; return the STORED value (Spec M5, B1).

    RLS-scoped to the caller's own row (the engine is already pinned), and returns what
    the DB actually holds rather than the requested value — so a caller with no
    subscription row (never provisioned) reads back ``False`` instead of a phantom
    ``True`` the engine would never honour.

    Eligibility is NOT decided here: the route enforces it with the same predicate the
    auto-top-up engine checks (D-M5-28), so the API can never persist an armed toggle
    for a plan the engine will silently refuse.
    """
    with rls_engine.begin() as conn:
        conn.execute(
            update(_sub_t)
            .where(_sub_t.c.user_id == user_id)
            .values(auto_topup_enabled=enabled, updated_at=text("now()"))
        )
        stored = conn.execute(
            select(_sub_t.c.auto_topup_enabled).where(_sub_t.c.user_id == user_id)
        ).scalar_one_or_none()
    return bool(stored)
