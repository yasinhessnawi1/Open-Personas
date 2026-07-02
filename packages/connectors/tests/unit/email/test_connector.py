"""The EmailConnector (Spec C5, Group C) — C1 Connector + C0 MessageDeliverer, over a fake client.

Proves: the two send shapes (originated = generated subject; reply = single ``Re:`` + threading),
the FREE C0 channel (``deliver`` sends with no window, recipient from the owner's linked email;
no linked email → pending), and the never-silent outcome mapping (rate-limit → pending, api
error → failed). The real Postmark send is the R4 live leg.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.delivery import DeliveryOutcome, MessageDeliverer
from persona.schema.origination import OriginatedMessage, PersonaIdentityTag
from persona_connectors.domain.normalise import NormalisedOutbound
from persona_connectors.domain.protocol import Connector
from persona_connectors.email.connector import EMAIL_CAPABILITIES, PLATFORM, EmailConnector
from persona_connectors.errors import PostmarkApiError, PostmarkRateLimitError

if TYPE_CHECKING:
    from collections.abc import Callable

_ASTRID = PersonaIdentityTag(persona_id="astrid", display_name="Astrid", visual_ref=None)


class _FakeClient:
    """Records send_email calls; optionally raises to exercise the outcome mapping."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def send_email(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


def _connector(
    client: _FakeClient, *, recipient: Callable[[str], str | None] | None = None
) -> EmailConnector:
    return EmailConnector(
        client=client,  # type: ignore[arg-type]
        from_address="inbound@personas.app",
        recipient_for=recipient if recipient is not None else (lambda _owner: "user@x.com"),
    )


def _originated(conversation_id: str | None = "conv-1") -> OriginatedMessage:
    return OriginatedMessage(
        persona=_ASTRID,
        owner_user_id="user_a",
        content="I've finished the report.",
        conversation_id=conversation_id,
        created_at=datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC),
    )


def test_connector_satisfies_both_protocols_and_the_free_capability() -> None:
    connector = _connector(_FakeClient())
    assert isinstance(connector, Connector)
    assert isinstance(connector, MessageDeliverer)
    assert connector.platform == PLATFORM
    assert EMAIL_CAPABILITIES.can_initiate_freely is True  # the freest C0 channel — no window
    assert EMAIL_CAPABILITIES.supports_author_affordance is True  # From display-name tier
    assert EMAIL_CAPABILITIES.is_realtime_push is False  # async
    assert EMAIL_CAPABILITIES.requires_delivery_auth is True  # SPF/DKIM/DMARC


@pytest.mark.asyncio
async def test_send_reply_uses_re_subject_author_from_and_threading() -> None:
    client = _FakeClient()
    result = await _connector(client).send_reply(
        to="user@x",
        original_subject="Deposit dispute",
        in_reply_to="<r@x>",
        references="<root@x>",
        persona=_ASTRID,
        text="Here's what I found.",
    )
    assert result.outcome == DeliveryOutcome.DELIVERED
    call = client.calls[0]
    assert call["subject"] == "Re: Deposit dispute"
    assert call["to"] == "user@x"
    assert call["from_"] == "Astrid via Open Persona <inbound@personas.app>"
    assert ("In-Reply-To", "<r@x>") in call["headers"]


@pytest.mark.asyncio
async def test_send_originated_uses_the_name_identified_subject() -> None:
    client = _FakeClient()
    outbound = NormalisedOutbound(persona=_ASTRID, text="done", conversation_key="user@x")
    result = await _connector(client).send(outbound)
    assert result.outcome == DeliveryOutcome.DELIVERED
    assert client.calls[0]["subject"] == "A message from Astrid"
    assert client.calls[0]["to"] == "user@x"


@pytest.mark.asyncio
async def test_deliver_is_the_free_channel_recipient_from_the_owner_link() -> None:
    """C0 originated email: no window check, recipient resolved from the owner's linked email."""
    client = _FakeClient()
    connector = _connector(client, recipient=lambda owner: f"{owner}@mail.com")
    result = await connector.deliver(_originated())
    assert result.outcome == DeliveryOutcome.DELIVERED
    assert client.calls[0]["to"] == "user_a@mail.com"  # sent freely, no window


@pytest.mark.asyncio
async def test_deliver_without_a_linked_email_is_pending_not_lost() -> None:
    client = _FakeClient()
    connector = _connector(client, recipient=lambda _owner: None)
    result = await connector.deliver(_originated())
    assert result.outcome == DeliveryOutcome.PENDING
    assert client.calls == []  # nothing sent


@pytest.mark.asyncio
async def test_rate_limit_maps_to_pending() -> None:
    connector = _connector(_FakeClient(error=PostmarkRateLimitError("slow down")))
    result = await connector.send(
        NormalisedOutbound(persona=_ASTRID, text="t", conversation_key="u@x")
    )
    assert result.outcome == DeliveryOutcome.PENDING


@pytest.mark.asyncio
async def test_api_error_maps_to_failed_never_silent() -> None:
    connector = _connector(_FakeClient(error=PostmarkApiError("rejected")))
    result = await connector.send(
        NormalisedOutbound(persona=_ASTRID, text="t", conversation_key="u@x")
    )
    assert result.outcome == DeliveryOutcome.FAILED
