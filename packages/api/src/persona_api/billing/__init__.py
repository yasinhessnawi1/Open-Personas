"""Stripe payments surface (Spec M4, T2) — persona-api-only, constructed dark.

The Stripe integration lives ENTIRELY in persona-api (persona-core stays
provider-agnostic + MIT-clean). Everything here is built ONLY when
``APIConfig.stripe_billing_active()`` is true (cloud edition + the
``PERSONA_BILLING_STRIPE_ENABLED`` flag + a secret key); community and flag-off
construct zero Stripe and never import the SDK (``StripeGateway`` lazy-imports it).
"""

from __future__ import annotations

from persona_api.billing.events import EventDispatcher, WebhookContext, build_event_dispatcher
from persona_api.billing.gateway import StripeGateway
from persona_api.billing.webhook import WebhookVerificationError, verify_and_dispatch

__all__ = [
    "EventDispatcher",
    "StripeGateway",
    "WebhookContext",
    "WebhookVerificationError",
    "build_event_dispatcher",
    "verify_and_dispatch",
]
