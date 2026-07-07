"""Connector-side A7 emission hooks — the birth points for message/link events (Spec A7, T6).

The connector service is a separate, api-coupled process; its api-free domain/adapter modules must
not import the dispatcher. So these helpers build **plain callbacks** (over primitives) that the
connector composition root (``persona_connectors.__main__``) injects into ``SharedInboundFlow`` and
``LinkingService`` — the callback closes over a real :class:`~persona_api.events.EventDispatcher`,
the domain code stays engine-free. Everything is gated on ``EventTriggerSettings().enabled``.

- :func:`make_message_received_emit` — the inbound convergence emits ``connector.message_received``.
- :func:`make_connector_linked_emit` — ``redeem_and_bind`` emits ``connector.linked``.
- :func:`on_connector_unlinked` — the API unlink route emits ``connector.unlinked`` AND disables
  the platform's triggers (criterion 7): fire any "when X unlinks" trigger once, then go dormant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from persona.events import ConnectorLinked, ConnectorMessageReceived, ConnectorUnlinked
from persona.events import EventTriggerSettings as _Settings

from persona_api.events.store import EventTriggerStore
from persona_api.events.wiring import build_event_dispatcher

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Engine

    from persona_api.config import APIConfig
    from persona_api.events.dispatcher import EventDispatcher

__all__ = [
    "ConnectorLinkedEmit",
    "MessageReceivedEmit",
    "make_connector_linked_emit",
    "make_message_received_emit",
    "on_connector_unlinked",
]


class MessageReceivedEmit(Protocol):
    """The inbound-emit callback ``SharedInboundFlow`` calls for a delivered turn (primitives)."""

    def __call__(
        self,
        *,
        owner_id: str,
        persona_id: str,
        conversation_id: str,
        platform: str,
        sender_id: str,
        thread_id: str | None,
        subject: str | None,
        body: str,
        message_id: str,
        occurred_at: datetime,
    ) -> None: ...


class ConnectorLinkedEmit(Protocol):
    """The link-emit callback ``LinkingService.redeem_and_bind`` calls after a successful bind."""

    def __call__(self, *, owner_id: str, platform: str, occurred_at: datetime) -> None: ...


def make_message_received_emit(dispatcher: EventDispatcher) -> MessageReceivedEmit:
    """Build the ``connector.message_received`` emitter (dispatch is synchronous + owner-scoped)."""

    def _emit(
        *,
        owner_id: str,
        persona_id: str,
        conversation_id: str,
        platform: str,
        sender_id: str,
        thread_id: str | None,
        subject: str | None,
        body: str,
        message_id: str,
        occurred_at: datetime,
    ) -> None:
        dispatcher.dispatch(
            ConnectorMessageReceived(
                event_id=message_id,  # the platform-stable dedup key (re-delivery re-keys)
                owner_id=owner_id,
                occurred_at=occurred_at,
                platform=platform,
                sender_id=sender_id,
                thread_id=thread_id,
                subject=subject,
                body=body,
                conversation_id=conversation_id,
                persona_id=persona_id,
                message_id=message_id,
            ),
            now=occurred_at,
        )

    return _emit


def make_connector_linked_emit(dispatcher: EventDispatcher) -> ConnectorLinkedEmit:
    """Build the ``connector.linked`` emitter for ``LinkingService.redeem_and_bind``."""

    def _emit(*, owner_id: str, platform: str, occurred_at: datetime) -> None:
        dispatcher.dispatch(
            ConnectorLinked(
                event_id=f"connector.linked:{platform}:{owner_id}:{occurred_at.isoformat()}",
                owner_id=owner_id,
                occurred_at=occurred_at,
                platform=platform,
            ),
            now=occurred_at,
        )

    return _emit


def on_connector_unlinked(
    *, rls_engine: Engine, config: APIConfig, owner_id: str, platform: str, now: datetime
) -> int:
    """API unlink hygiene (criterion 7): fire any unlink-trigger, THEN disable the platform's.

    Emits ``connector.unlinked`` first so a "when {platform} unlinks" trigger fires once on the
    real unlink, then :meth:`disable_for_platform` sends every one of that platform's triggers
    dormant (``disabled_reason='unlinked'``; no silent re-enable on re-link). No-op (returns 0) when
    event triggers are disabled. Returns the number of triggers disabled.
    """
    if not _Settings().enabled:
        return 0
    dispatcher = build_event_dispatcher(rls_engine=rls_engine, config=config)
    dispatcher.dispatch(
        ConnectorUnlinked(
            event_id=f"connector.unlinked:{platform}:{owner_id}:{now.isoformat()}",
            owner_id=owner_id,
            occurred_at=now,
            platform=platform,
        ),
        now=now,
    )
    return EventTriggerStore(rls_engine).disable_for_platform(owner_id, platform, now=now)
