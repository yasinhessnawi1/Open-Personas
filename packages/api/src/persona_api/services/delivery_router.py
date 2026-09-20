"""Delivery routing for originated messages (Spec C0, T6, D-C0-2).

:class:`DeliveryRouter` is the one-pipe-many-deliverers facade: it holds a registry
of :class:`~persona.delivery.MessageDeliverer` channels and, given an originated
message, picks **exactly one** target channel and dispatches to it. It is itself a
``MessageDeliverer`` — the composite the ``Originator`` (T3) depends on, so direction
4 later drives *this one boundary*, never N channel-specific paths.

The v1 default policy (D-C0-2): route to the channel the relevant conversation lives
on, with the **web app as the always-available home**. Two guarantees hold *by
construction*:

* **No double-delivery** — one channel key is resolved and one ``deliver`` is awaited.
  There is no fan-out. A channel that REPORTS it did not deliver (``pending`` /
  ``failed``) hands the message to the home channel once (R9-120), which is a second
  attempt rather than a second delivery: at most one channel ever reaches the person,
  because the hop runs only when the first said it did not.
* **No silent drop** — the home channel must be registered (a fail-fast construction
  guard), and an unresolved/unknown channel key falls back to it, so there is always
  a target. (Undeliverable-right-now is the deliverer's ``pending`` outcome, D-C0-4 —
  not a drop.)

Neither guarantee is a claim that the person SAW the message. ``pending`` and the home
hop are statements about our records: the message is durable and reachable. Whether it
was read is not something this layer knows, and no text it produces may imply it does.

Channel selection is an **injected resolver + dict lookup** — never an ``if
platform == …`` switch (D-08-3: the API treats the connector ``channel`` as opaque).
In v1 only the web channel exists, so the resolver returns the home; C1 supplies a
resolver that maps the conversation's channel descriptor to a registered channel key
(still a lookup, still no platform branching). Routing decision + outcome are tracked
at this layer via the api audit log (D-C0-5), distinct from the deliverer's
channel-level outcome record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.delivery import DeliveryOutcome

from persona_api.services import audit_service

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.delivery import DeliveryResult, MessageDeliverer
    from persona.schema.origination import OriginatedMessage
    from sqlalchemy import Engine

#: The always-available fallback channel. Public because the origination composition
#: (:mod:`persona_api.services.origination_delivery`) must name the same channel this
#: router falls back to, and two string literals for one concept is exactly how a
#: registry and its home drift apart.
HOME_CHANNEL = "web"

_DEFAULT_HOME = HOME_CHANNEL


def _home_only(message: OriginatedMessage) -> str | None:  # noqa: ARG001 — v1 resolver
    """v1 channel resolver: always the home channel (only the web channel exists)."""
    return None


class DeliveryRouter:
    """Route an originated message to exactly one registered deliverer (D-C0-2).

    Args:
        deliverers: Channel-key → :class:`MessageDeliverer`. Must include
            ``home_channel``.
        rls_engine: The engine the routing audit write runs on.
        home_channel: The always-available fallback channel (default ``"web"``).
        resolve_channel: Picks the channel key for a message (``None`` → home). v1
            default is home-only; C1 injects a conversation-channel resolver.
        record: The audit sink (injected for testability; defaults to the api audit
            service).

    Raises:
        ValueError: If ``home_channel`` is not in ``deliverers`` — a fail-fast guard
            so routing always has a target (no silent drop).
    """

    def __init__(
        self,
        *,
        deliverers: dict[str, MessageDeliverer],
        rls_engine: Engine,
        home_channel: str = _DEFAULT_HOME,
        resolve_channel: Callable[[OriginatedMessage], str | None] = _home_only,
        record: Callable[..., None] = audit_service.record,
    ) -> None:
        if home_channel not in deliverers:
            msg = (
                f"home_channel {home_channel!r} has no registered deliverer; "
                f"registered: {sorted(deliverers)}"
            )
            raise ValueError(msg)
        self._deliverers = dict(deliverers)
        self._engine = rls_engine
        self._home = home_channel
        self._resolve = resolve_channel
        self._record = record

    async def deliver(self, message: OriginatedMessage) -> DeliveryResult:
        """Resolve one channel, dispatch, and fall back to home once if it did not take.

        R9-120: ``pending`` used to mean one thing everywhere, "durably persisted, present
        on next open". That is true of the web app, where the conversation is a place the
        person returns to. It is not true of a chat app: nobody "opens" a Telegram
        conversation to check for something they were never told about. So a channel that
        reports it did not deliver gets ONE hop to the home channel, which is always
        registered and where the durable record already lives.

        Exactly one hop, no retry and no outbox, and the invariant survives: at most one
        channel ever delivers, because the hop runs only when the first attempt reported
        that it did not. A deliverer reports ``failed`` for a rejection and ``pending``
        for "not right now"; a genuine fault raises instead of returning, so an ambiguous
        send is never double-delivered by this path.

        The hop is not a promise that anyone saw it. It makes the message durable and
        reachable through a channel the person can return to; whether they do is not
        something this layer can claim.
        """
        key = self._resolve(message)
        channel = key if (key is not None and key in self._deliverers) else self._home
        result = await self._deliverers[channel].deliver(message)
        if channel == self._home or result.outcome is DeliveryOutcome.DELIVERED:
            self._audit_routing(message, channel, result)
            return result
        home_result = await self._deliverers[self._home].deliver(message)
        self._audit_routing(message, self._home, home_result, attempted=(channel, result))
        return home_result

    def _audit_routing(
        self,
        message: OriginatedMessage,
        channel: str,
        result: DeliveryResult,
        *,
        attempted: tuple[str, DeliveryResult] | None = None,
    ) -> None:
        """Record one routing decision. ``attempted`` adds the hop's origin when there was one.

        One row per originated message either way, because one message is one routing
        decision. The two extra keys appear only on a hop, so their presence IS the signal
        that a channel was tried and did not take; a reader does not have to correlate rows
        to find out.
        """
        metadata = {
            "persona_id": message.persona.persona_id,
            "channel": channel,
            "outcome": result.outcome.value,
        }
        if attempted is not None:
            attempted_channel, attempted_result = attempted
            metadata["attempted_channel"] = attempted_channel
            metadata["attempted_outcome"] = attempted_result.outcome.value
        self._record(
            engine=self._engine,
            user_id=message.owner_user_id,
            action="origination.routing",
            target=message.conversation_id or "",
            metadata=metadata,
        )
