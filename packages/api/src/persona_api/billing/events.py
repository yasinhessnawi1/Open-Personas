"""Stripe webhook event router (Spec M4, T3a).

Routes a VERIFIED event to its per-type handler. Handlers land in T3b (subscription
lifecycle + PAYG grant); T3a ships the skeleton with an empty registry, so every event
is currently an "unhandled type → no-op (200)". The idempotency anchor is the Stripe
``event.id`` (a re-delivered event carries the SAME id): handlers use it as the ledger
``billing_key`` on ``grant_idempotent`` / ``deduct_idempotent``, so a re-delivery grants
exactly once (the T1b/T3b guarantee). Unknown types are ignored (200), never 4xx'd —
a 4xx makes Stripe retry forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import stripe
    from sqlalchemy import Engine

    from persona_api.config import APIConfig
    from persona_api.editions.credits_policy import CreditsPolicy

__all__ = ["EventDispatcher", "WebhookContext", "build_event_dispatcher"]

_log = get_logger("api.billing.events")


@dataclass(frozen=True)
class WebhookContext:
    """The dependencies a webhook handler needs to apply an event (Spec M4, T3a/T3b).

    Threaded from the route (``app.state``) into each handler. **Security invariant
    (Option A):** ``admin_engine`` is used ONLY for the one read-only ``stripe_customer_id
    → user_id`` resolve; EVERY write goes through ``rls_engine`` with ``current_user_id``
    set to the resolved user, so RLS enforces the scope. ``config`` supplies the price→plan
    reverse map.
    """

    rls_engine: Engine
    admin_engine: Engine
    credits_policy: CreditsPolicy
    config: APIConfig


# A handler applies one verified event: ``(event, context) -> None``. It reads
# ``event.id`` for its idempotency ``billing_key`` (a re-delivery is a no-op); raising
# propagates to the route (→ 5xx, so Stripe retries). Handlers are T3b.


class EventDispatcher:
    """Routes a verified Stripe event to its per-type handler (Spec M4, T3a).

    Constructed with an immutable ``{event_type: handler}`` registry. An event whose
    type has no handler is a no-op (returns ``False``) — the route still answers 200 so
    Stripe does not retry an intentionally-ignored type.
    """

    def __init__(
        self, handlers: Mapping[str, Callable[[stripe.Event, WebhookContext], None]]
    ) -> None:
        self._handlers: dict[str, Callable[[stripe.Event, WebhookContext], None]] = dict(handlers)

    def dispatch(self, event: stripe.Event, context: WebhookContext) -> bool:
        """Dispatch ``event`` to its handler. Returns whether a handler ran.

        Unknown type → logs + returns ``False`` (the route answers 200 no-op). The
        ``event.id`` is the idempotency anchor the handler uses (logged here so a
        re-delivery is visible in the audit trail).
        """
        handler = self._handlers.get(event.type)
        if handler is None:
            _log.info(
                "ignoring unhandled stripe event type", event_type=event.type, event_id=event.id
            )
            return False
        _log.info("dispatching stripe event", event_type=event.type, event_id=event.id)
        handler(event, context)
        return True


def build_event_dispatcher() -> EventDispatcher:
    """The webhook handler registry (Spec M4). T3b wires the subscription-lifecycle
    handlers: ``checkout.session.completed`` / ``customer.subscription.updated`` /
    ``customer.subscription.deleted`` / ``invoice.paid`` / ``invoice.payment_failed``
    (``payment_intent.succeeded`` for PAYG lands at T4). Lazy import avoids a module cycle
    (handlers reference :class:`WebhookContext`)."""
    from persona_api.billing.handlers import lifecycle_handlers  # noqa: PLC0415

    return EventDispatcher(lifecycle_handlers())
