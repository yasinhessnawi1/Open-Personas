"""WhatsAppFlowTransport + decline copy (Spec C4 T5).

The transport satisfies C1's :class:`~persona_connectors.domain.flow.FlowTransport`
Protocol over a fake client/connector: ``send_system`` plain-text, ``send_persona``
delegating to the connector's ``send``, and ``typing`` — a NO-OP context manager
(Twilio exposes no WhatsApp typing API in the Messages REST API; the capability flag
reflects the platform, not the transport). The decline copy is exercised here too.
"""

from __future__ import annotations

import pytest
from persona.delivery import DeliveryOutcome, DeliveryResult
from persona.schema.origination import PersonaIdentityTag
from persona_connectors._twilio.client import TwilioMessageResult
from persona_connectors.domain.flow import FlowTransport
from persona_connectors.domain.normalise import NormalisedOutbound
from persona_connectors.whatsapp.flow import WhatsAppFlowTransport
from persona_connectors.whatsapp.inbound import NonTextKind
from persona_connectors.whatsapp.non_text import decline_message


class _FakeClient:
    def __init__(self) -> None:
        self.sends: list[tuple[str, str, str | None]] = []

    async def send_message(
        self, *, to: str, from_: str, body: str | None = None
    ) -> TwilioMessageResult:
        self.sends.append((to, from_, body))
        return TwilioMessageResult(sid="SM1", status="queued")


class _FakeConnector:
    def __init__(self) -> None:
        self.persona_sends: list[NormalisedOutbound] = []

    async def send(self, outbound: NormalisedOutbound) -> DeliveryResult:
        self.persona_sends.append(outbound)
        return DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel="whatsapp")


def _transport() -> tuple[WhatsAppFlowTransport, _FakeClient, _FakeConnector]:
    client = _FakeClient()
    connector = _FakeConnector()
    transport = WhatsAppFlowTransport(
        client=client, connector=connector, from_address="whatsapp:+14155238886"
    )
    return transport, client, connector


def test_transport_satisfies_the_flow_transport_protocol() -> None:
    """WhatsAppFlowTransport is a structural FlowTransport (so the shared flow accepts it)."""
    transport, _client, _connector = _transport()
    assert isinstance(transport, FlowTransport)


@pytest.mark.asyncio
async def test_send_system_posts_plain_text_with_whatsapp_prefix() -> None:
    """send_system sends via Twilio, re-applying the whatsapp: prefix + the from-address."""
    transport, client, _connector = _transport()
    await transport.send_system(conversation_key="+14155551234", text="link your account")
    assert client.sends == [("whatsapp:+14155551234", "whatsapp:+14155238886", "link your account")]


@pytest.mark.asyncio
async def test_send_persona_delegates_to_the_connector() -> None:
    """send_persona hands the outbound to connector.send (T8/T13 own the body)."""
    transport, _client, connector = _transport()
    tag = PersonaIdentityTag(persona_id="p1", display_name="Astrid", visual_ref=None)
    outbound = NormalisedOutbound(persona=tag, text="hi", conversation_key="+1")
    await transport.send_persona(outbound)
    assert connector.persona_sends == [outbound]


@pytest.mark.asyncio
async def test_typing_is_a_noop_context_manager() -> None:
    """Twilio has no WhatsApp typing REST API → typing is a no-op (no client calls)."""
    transport, client, _connector = _transport()
    async with transport.typing("+14155551234"):
        pass
    assert client.sends == []  # no side effect


def test_decline_message_covers_every_kind() -> None:
    """Each NonTextKind maps to a non-empty product-voice decline line."""
    for kind in NonTextKind:
        line = decline_message(kind)
        assert isinstance(line, str)
        assert line.strip()
