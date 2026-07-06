"""Spec A11 T2 — the ``LiveSessionRegistry`` adapter over :class:`UserEventChannel`.

Fills the seam ``WebAppDeliverer`` already consumes (``services/web_deliverer.py``):
the background origination path is wired to ``_NoLiveSessions`` today (always
``None`` → ``PENDING`` → reload, the R4-C1-23 bug). :class:`ChannelLiveSessions`
returns a real sink when the message's owner has an open tab AND the message targets
a conversation, so a background ``message.delivered`` pushes live onto the open chat;
with no tab it returns ``None`` and the deliverer's ``PENDING`` (present-on-open)
behaviour is unchanged. The seam contract is untouched — A11 only supplies a
non-null registry (T4 does the swap at the call site).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_api.realtime.events import MessageDeliveredEvent

if TYPE_CHECKING:
    from persona.schema.origination import OriginatedMessage

    from persona_api.realtime.channel import UserEventChannel

__all__ = ["ChannelLiveSessions"]


class _ChannelSink:
    """A :class:`~persona_api.services.web_deliverer.LiveSessionSink` that publishes a
    ``message.delivered`` onto the owner's live channel."""

    def __init__(self, channel: UserEventChannel) -> None:
        self._channel = channel

    async def push(self, message: OriginatedMessage) -> None:
        """Publish the originated message as a ``message.delivered`` event. Data-only:
        the client refetches the conversation to reconcile by id (A11-D-2). No-op if
        the message has no conversation target (should not happen — ``lookup`` guards
        it — but stays safe)."""
        if message.conversation_id is None:
            return
        self._channel.publish(
            message.owner_user_id,
            MessageDeliveredEvent(
                conversation_id=message.conversation_id,
                persona_id=message.persona.persona_id,
                persona_name=message.persona.display_name,
                visual_ref=message.persona.visual_ref,
            ),
        )


class ChannelLiveSessions:
    """A :class:`~persona_api.services.web_deliverer.LiveSessionRegistry` backed by the
    persistent user channel — the real registry that replaces ``_NoLiveSessions``."""

    def __init__(self, channel: UserEventChannel) -> None:
        self._channel = channel

    def lookup(self, message: OriginatedMessage) -> _ChannelSink | None:
        """Return a sink iff the owner has an open tab AND the message targets a
        conversation; else ``None`` (→ the deliverer marks PENDING, present-on-open)."""
        if message.conversation_id is None:
            return None
        if not self._channel.has_subscribers(message.owner_user_id):
            return None
        return _ChannelSink(self._channel)
