"""The SMS flow transport (Spec C4 T5) — the SMS surface I/O over the shared flow.

This module supplies SMS's :class:`~persona_connectors.domain.flow.FlowTransport` — the
per-platform I/O the shared C1 flow (``domain/flow.py``) needs: a plain system send, the
persona reply via the connector's ``send``, and a NO-OP ``typing`` (SMS has no typing
indicator — mirror ``slack/flow.py``'s ``_no_typing``). The platform-agnostic sequence
(resolve → ``/new`` → route → drive → send) is C1's
:class:`~persona_connectors.domain.flow.SharedInboundFlow`; the full inbound orchestrator
(classify + the non-text decline + the phone-linking auth carrier + the shared-flow
delegation) lands in a later task (T6+).

**api-free**: the persona reply delegates to the connector's ``send``. Mirrors
``slack/flow.py``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.delivery import DeliveryResult

    from persona_connectors._twilio.client import TwilioMessageResult
    from persona_connectors.domain.normalise import NormalisedOutbound

__all__ = ["SmsFlowTransport"]


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
    """A no-op typing window — SMS has no typing indicator (mirror slack/flow.py)."""
    yield


class SmsFlowTransport:
    """SMS's :class:`~persona_connectors.domain.flow.FlowTransport` — pure I/O; typing is a no-op.

    Lowers the shared flow's three I/O needs to SMS: a plain system send (the bot
    speaking — a direct Twilio ``send_message`` to the bare-E.164 ``conversation_key``),
    the persona reply via the connector (the plain-prefix render + the multi-segment
    split land in the connector's ``send``, T12), and a no-op typing window. The
    ``from_address`` is the configured ``twilio_sms_from`` (bare ``+E164``). Dependencies
    are injected (DI; no globals).
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

    async def send_system(self, *, conversation_key: str, text: str) -> None:
        await self._client.send_message(to=conversation_key, from_=self._from, body=text)

    async def send_persona(self, outbound: NormalisedOutbound) -> None:
        await self._connector.send(outbound)

    def typing(self, conversation_key: str) -> contextlib.AbstractAsyncContextManager[None]:
        return _no_typing(conversation_key)
