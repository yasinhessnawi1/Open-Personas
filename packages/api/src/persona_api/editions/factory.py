"""Edition selection (Spec 33, D-33-1): build the seam impls from config.

A single ``PERSONA_EDITION`` switch picks the ``OwnerResolver`` and
``CreditsPolicy`` implementations at the app factory. Call sites consume the
selected interface from ``app.state`` — no scattered ``if edition`` checks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.logging import get_logger

from persona_api.config import Edition
from persona_api.editions.credits_policy import (
    CreditsPolicy,
    MeteredCreditsPolicy,
    UnlimitedCreditsPolicy,
)
from persona_api.editions.owner_resolver import (
    CloudOwnerResolver,
    CommunityOwnerResolver,
    OwnerResolver,
)

if TYPE_CHECKING:
    from persona_api.billing import StripeGateway
    from persona_api.config import APIConfig

__all__ = ["build_credits_policy", "build_owner_resolver", "build_stripe_gateway"]

_log = get_logger("api.editions.factory")


def build_owner_resolver(config: APIConfig) -> OwnerResolver:
    """The edition's request-ownership resolver (§2.1)."""
    if config.edition is Edition.cloud:
        return CloudOwnerResolver()
    return CommunityOwnerResolver(
        owner_id=config.community_owner_id, email=config.community_owner_email
    )


def build_credits_policy(config: APIConfig) -> CreditsPolicy:
    """The edition's credits policy (§2.2).

    Spec R7 (R7-D-6): cloud's metered policy carries the per-UTC-day spend cap
    (``CREDITS_MAX_PER_DAY``); community's :class:`UnlimitedCreditsPolicy` no-ops, so
    the cap never applies to a self-host install.
    """
    if config.edition is Edition.cloud:
        return MeteredCreditsPolicy(daily_cap=config.credits_max_per_day)
    return UnlimitedCreditsPolicy()


def build_stripe_gateway(config: APIConfig) -> StripeGateway | None:
    """The Stripe gateway when billing is ACTIVE, else ``None`` (Spec M4, T2a).

    Mirrors :func:`build_credits_policy`'s edition seam: billing is constructed ONLY
    when ``config.stripe_billing_active()`` (cloud + ``PERSONA_BILLING_STRIPE_ENABLED``
    + a secret key). Community and flag-off return ``None`` — ZERO Stripe, and the
    ``stripe`` SDK is never imported (``StripeGateway`` lazy-imports it, and this
    factory only imports+constructs it on the active path). M4 ships dark until the
    owner supplies live keys and flips the flag.
    """
    if not config.stripe_billing_active():
        return None
    from persona_api.billing import StripeGateway  # noqa: PLC0415 — active path only
    from persona_api.billing.autotopup import AUTO_TOPUP_PACK_KEY  # noqa: PLC0415

    # R9-177 B9: the off-session auto top-up bills the SAME pack Price the Checkout path
    # sells, as a taxed invoice. Unset means the top-up refuses at the call, loudly, rather
    # than inventing an untaxed amount; say so once at boot instead of once per crossing.
    autotopup_price_id = config.stripe_price_for_pack(AUTO_TOPUP_PACK_KEY)
    if not autotopup_price_id:
        _log.warning(
            "billing is active but the auto top-up pack Price is not configured; "
            "auto top-ups will not charge until PERSONA_STRIPE_PRICE_PACK_{} is set",
            AUTO_TOPUP_PACK_KEY,
        )
    return StripeGateway(
        secret_key=config.stripe_secret_key,
        publishable_key=config.stripe_publishable_key,
        webhook_secret=config.stripe_webhook_secret,
        autotopup_price_id=autotopup_price_id,
    )
