"""The WhatsApp flow transport (Spec C4 T5) — the WhatsApp surface I/O over the shared flow.

This module supplies WhatsApp's :class:`~persona_connectors.domain.flow.FlowTransport`
— the per-platform I/O the shared C1 flow (``domain/flow.py``) needs: a plain system
send, the persona reply via the connector's ``send``, and the ``typing`` working
indicator. The platform-agnostic sequence (resolve → ``/new`` → route → drive → send)
is C1's :class:`~persona_connectors.domain.flow.SharedInboundFlow`, shared with
Telegram / Discord / Slack; the full inbound orchestrator (classify + the non-text
decline + the phone-linking auth carrier + the shared-flow delegation) lands in a
later task (T6+).

**Typing is a no-op (for now).** WhatsApp's *platform* capability flag is
``supports_typing_indicator=True``, but Twilio's Messages REST API does NOT expose a
WhatsApp typing API — so the no-streaming reply just sends whole (mirroring Slack's
``_no_typing``). Revisit if/when this moves to the Meta-direct Cloud API, which does
expose a typing indicator.

**api-free**: the persona reply delegates to the connector's ``send`` (the api-coupling,
if any, is owned by the connector + the composition root). Mirrors ``slack/flow.py``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Protocol

from persona_connectors.whatsapp.inbound import WHATSAPP_PREFIX

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.delivery import DeliveryResult

    from persona_connectors._twilio.client import TwilioMessageResult
    from persona_connectors.domain.normalise import NormalisedOutbound

__all__ = ["WhatsAppFlowTransport"]


class _SystemSender(Protocol):
    """The minimal Twilio-client seam the transport needs for a plain system send.

    Structural so the test fake (and the real
    :class:`~persona_connectors._twilio.client.TwilioClient`) both satisfy it.
    """

    async def send_message(
        self, *, to: str, from_: str, body: str | None = ...
    ) -> TwilioMessageResult: ...


class _PersonaSender(Protocol):
    """The minimal connector seam the transport needs for the persona reply (``send``)."""

    async def send(self, outbound: NormalisedOutbound) -> DeliveryResult: ...


@contextlib.asynccontextmanager
async def _no_typing(_conversation_key: str) -> AsyncIterator[None]:
    """A no-op typing window — Twilio exposes no WhatsApp typing REST API (T5 note)."""
    yield


class WhatsAppFlowTransport:
    """WhatsApp's :class:`~persona_connectors.domain.flow.FlowTransport` — pure I/O, no auth.

    Lowers the shared flow's three I/O needs to WhatsApp: a plain system send (the bot
    speaking — a direct Twilio ``send_message`` with the ``whatsapp:`` prefix re-applied
    to the bare-E.164 ``conversation_key``), the persona reply via the connector (the
    render + 1600-char split + the 24h-window gate land in the connector's ``send``,
    T13), and a no-op typing window (Twilio has no WhatsApp typing API). The
    ``from_address`` is the configured ``twilio_whatsapp_from`` (``whatsapp:+E164``).
    Dependencies are injected (DI; no globals).
    """

    def __init__(
        self,
        *,
        client: _SystemSender,
        connector: _PersonaSender,
        from_address: str = "",
    ) -> None:
        self._client = client
        self._connector = connector
        self._from = from_address

    @staticmethod
    def _to_address(conversation_key: str) -> str:
        """Re-apply Twilio's ``whatsapp:`` prefix to a bare-E.164 conversation key."""
        if conversation_key.startswith(WHATSAPP_PREFIX):
            return conversation_key
        return f"{WHATSAPP_PREFIX}{conversation_key}"

    async def send_system(self, *, conversation_key: str, text: str) -> None:
        await self._client.send_message(
            to=self._to_address(conversation_key), from_=self._from, body=text
        )

    async def send_persona(self, outbound: NormalisedOutbound) -> None:
        await self._connector.send(outbound)

    def typing(self, conversation_key: str) -> contextlib.AbstractAsyncContextManager[None]:
        return _no_typing(conversation_key)
