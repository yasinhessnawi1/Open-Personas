"""WhatsAppConnector — C1 Connector + C0 MessageDeliverer (Spec C4 T8 send, T9 reactive).

T8 (in-window send): render the `*bold*` name tag + 1600-char split, lower to Twilio's
Messages API with the ``whatsapp:`` prefix, map the outcome — ``delivered`` on success,
``pending`` on a rate-limit, ``failed`` on a rejection (never silent).

T9 (the reactive core): a create-time **sync** out-of-window code (63016/131047) routes
through the shared ``map_delivery`` to the window-distinct ``failed`` detail (the same
recognition the async status-callback uses); the C0 ``deliver`` GAP-A bridge resolves a
conversation to its channel, and a missing channel → ``pending`` (content persisted
upstream, never lost). The real async-rejection shape is the named live-validation leg.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.delivery import DeliveryOutcome, MessageDeliverer
from persona.schema.origination import OriginatedMessage, PersonaIdentityTag
from persona_connectors._twilio.client import TwilioMessageResult
from persona_connectors._twilio.status import WINDOW_CLOSED_DETAIL
from persona_connectors.domain.conversation_model import ChannelRef
from persona_connectors.domain.normalise import NormalisedOutbound
from persona_connectors.domain.protocol import Connector
from persona_connectors.errors import TwilioApiError, TwilioRateLimitError
from persona_connectors.whatsapp.connector import WhatsAppConnector

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_PERSONA = PersonaIdentityTag(persona_id="p1", display_name="Astrid", visual_ref=None)


class _FakeClient:
    """A structural Twilio client — records calls, returns queued results (or raises)."""

    def __init__(
        self,
        *,
        results: Sequence[TwilioMessageResult] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, str | None]] = []
        self._results = list(results) if results else []
        self._raises = raises

    async def send_message(
        self,
        *,
        to: str,
        from_: str,
        body: str | None = None,
        content_sid: str | None = None,
        content_variables: str | None = None,
        status_callback: str | None = None,
    ) -> TwilioMessageResult:
        self.calls.append(
            {
                "to": to,
                "from_": from_,
                "body": body,
                "content_sid": content_sid,
                "content_variables": content_variables,
                "status_callback": status_callback,
            }
        )
        if self._raises is not None:
            raise self._raises
        if self._results:
            return self._results.pop(0)
        return TwilioMessageResult(sid="SM1", status="queued", error_code=None)


class _FakeStore:
    """A minimal ConversationStateStore stand-in exposing only resolve_channel."""

    def __init__(self, ref: ChannelRef | None) -> None:
        self._ref = ref
        self.seen_conversation_id: str | None = None

    def resolve_channel(self, *, conversation_id: str) -> ChannelRef | None:
        self.seen_conversation_id = conversation_id
        return self._ref


def _scope_recorder(entered: list[str]) -> object:
    @contextlib.contextmanager
    def scope(owner_id: str) -> Iterator[None]:
        entered.append(owner_id)
        yield

    return scope


def _connector(
    client: _FakeClient,
    *,
    ref: ChannelRef | None = None,
    entered: list[str] | None = None,
    template_sid: str = "",
    time_sensitive: bool = False,
) -> WhatsAppConnector:
    return WhatsAppConnector(
        client=client,  # type: ignore[arg-type]
        from_address="whatsapp:+15550000000",
        conversation_store=_FakeStore(ref),  # type: ignore[arg-type]
        owner_scope=_scope_recorder(entered if entered is not None else []),  # type: ignore[arg-type]
        reengagement_template_sid=template_sid,
        is_time_sensitive=(lambda _m: True) if time_sensitive else None,
    )


def _outbound(text: str = "Hello", key: str = "+15551230000") -> NormalisedOutbound:
    return NormalisedOutbound(
        persona=_PERSONA, text=text, conversation_key=key, reply_to_message_id=None
    )


def _originated(conversation_id: str | None) -> OriginatedMessage:
    return OriginatedMessage(
        persona=_PERSONA,
        owner_user_id="user_a",
        content="I've finished the task.",
        conversation_id=conversation_id,
        created_at=datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC),
    )


# --- protocol conformance ---


def test_connector_satisfies_both_protocols() -> None:
    connector = _connector(_FakeClient())
    assert isinstance(connector, Connector)
    assert isinstance(connector, MessageDeliverer)
    assert connector.platform == "whatsapp"
    assert connector.capabilities.can_initiate_freely is False


# --- send (Connector), in-window (T8) ---


@pytest.mark.asyncio
async def test_send_in_window_delivers_and_applies_whatsapp_prefix() -> None:
    client = _FakeClient()
    result = await _connector(client).send(_outbound("Hi"))
    assert result.outcome is DeliveryOutcome.DELIVERED
    assert client.calls[0]["to"] == "whatsapp:+15551230000"  # bare-E.164 key → prefixed
    assert client.calls[0]["from_"] == "whatsapp:+15550000000"
    assert client.calls[0]["body"] == "*Astrid*\nHi"


@pytest.mark.asyncio
async def test_rate_limit_maps_to_pending_not_silent() -> None:
    client = _FakeClient(raises=TwilioRateLimitError("429", context={}))
    result = await _connector(client).send(_outbound())
    assert result.outcome is DeliveryOutcome.PENDING


@pytest.mark.asyncio
async def test_api_error_maps_to_failed_not_silent() -> None:
    client = _FakeClient(raises=TwilioApiError("boom", context={}))
    result = await _connector(client).send(_outbound())
    assert result.outcome is DeliveryOutcome.FAILED


@pytest.mark.asyncio
async def test_generic_create_time_error_code_maps_to_failed_with_code() -> None:
    client = _FakeClient(
        results=[TwilioMessageResult(sid="SM1", status="failed", error_code=21211)]
    )
    result = await _connector(client).send(_outbound())
    assert result.outcome is DeliveryOutcome.FAILED
    assert "21211" in (result.detail or "")  # observable
    assert result.detail != WINDOW_CLOSED_DETAIL  # a generic code is NOT the window path


@pytest.mark.asyncio
async def test_long_reply_sends_each_part_header_on_first_only() -> None:
    client = _FakeClient()
    await _connector(client).send(_outbound(" ".join(["word"] * 1000)))
    assert len(client.calls) >= 2
    first_body = client.calls[0]["body"] or ""
    second_body = client.calls[1]["body"] or ""
    assert first_body.startswith("*Astrid*\n")
    assert not second_body.startswith("*Astrid*")


# --- T9: the SYNC out-of-window code → window-distinct failed (the 2×2 sync cells) ---


@pytest.mark.asyncio
async def test_sync_window_code_63016_maps_to_window_detail() -> None:
    client = _FakeClient(
        results=[TwilioMessageResult(sid="SM1", status="failed", error_code=63016)]
    )
    result = await _connector(client).send(_outbound())
    assert result.outcome is DeliveryOutcome.FAILED
    assert result.detail == WINDOW_CLOSED_DETAIL  # the T10 template signal, on the sync path


@pytest.mark.asyncio
async def test_sync_window_code_131047_maps_to_window_detail() -> None:
    client = _FakeClient(
        results=[TwilioMessageResult(sid="SM1", status="failed", error_code=131047)]
    )
    result = await _connector(client).send(_outbound())
    assert result.detail == WINDOW_CLOSED_DETAIL


# --- T9: the C0 deliver GAP-A bridge (resolve_channel → send), never-lost ---


@pytest.mark.asyncio
async def test_deliver_resolves_channel_and_sends() -> None:
    client = _FakeClient()
    entered: list[str] = []
    ref = ChannelRef(platform="whatsapp", channel_key="+15551230000")
    result = await _connector(client, ref=ref, entered=entered).deliver(_originated("conv_1"))
    assert result.outcome is DeliveryOutcome.DELIVERED
    assert entered == ["user_a"]  # owner-scoped the reverse lookup
    assert client.calls[0]["to"] == "whatsapp:+15551230000"


@pytest.mark.asyncio
async def test_deliver_no_channel_is_pending_never_lost() -> None:
    # no resolvable WhatsApp channel → pending (content already persisted upstream, C0 D-C0-4)
    result = await _connector(_FakeClient(), ref=None).deliver(_originated("conv_x"))
    assert result.outcome is DeliveryOutcome.PENDING
    assert "no connector channel" in (result.detail or "")


@pytest.mark.asyncio
async def test_deliver_wrong_platform_channel_is_pending() -> None:
    ref = ChannelRef(platform="telegram", channel_key="123")  # someone else's channel
    result = await _connector(_FakeClient(), ref=ref).deliver(_originated("conv_y"))
    assert result.outcome is DeliveryOutcome.PENDING


@pytest.mark.asyncio
async def test_deliver_no_conversation_id_is_pending() -> None:
    result = await _connector(_FakeClient()).deliver(_originated(None))
    assert result.outcome is DeliveryOutcome.PENDING
    assert "no conversation" in (result.detail or "")


# --- T10: the ContentSid re-engagement template (built mechanism + conservative gate) ---


@pytest.mark.asyncio
async def test_send_reengagement_template_sends_content_sid_with_persona_variable() -> None:
    client = _FakeClient()
    conn = _connector(client, template_sid="HX123")
    result = await conn.send_reengagement_template("+15551230000", persona_name="Astrid")
    assert result.outcome is DeliveryOutcome.DELIVERED
    assert client.calls[0]["content_sid"] == "HX123"
    assert client.calls[0]["body"] is None  # a template send, NOT a free-form body
    assert "Astrid" in (client.calls[0]["content_variables"] or "")
    assert client.calls[0]["to"] == "whatsapp:+15551230000"


@pytest.mark.asyncio
async def test_send_reengagement_template_no_template_configured_is_failed_no_call() -> None:
    client = _FakeClient()
    conn = _connector(client, template_sid="")  # none configured
    result = await conn.send_reengagement_template("+15551230000")
    assert result.outcome is DeliveryOutcome.FAILED
    assert "no re-engagement template" in (result.detail or "")
    assert client.calls == []  # never hit Twilio


@pytest.mark.asyncio
async def test_deliver_window_closed_non_urgent_is_persist_only_no_template() -> None:
    # the DEFAULT: a window-closed C0, not time-sensitive → persist-only (no billable template)
    client = _FakeClient(
        results=[TwilioMessageResult(sid="SM1", status="failed", error_code=63016)]
    )
    ref = ChannelRef(platform="whatsapp", channel_key="+15551230000")
    conn = _connector(client, ref=ref, template_sid="HX123")  # template configured but non-urgent
    result = await conn.deliver(_originated("conv_1"))
    assert result.outcome is DeliveryOutcome.FAILED
    assert result.detail == WINDOW_CLOSED_DETAIL  # persist-only, present-on-next-open
    assert len(client.calls) == 1  # ONLY the free-form attempt — the template never fired
    assert client.calls[0]["content_sid"] is None


@pytest.mark.asyncio
async def test_deliver_window_closed_time_sensitive_fires_template() -> None:
    # the opt-in: a window-closed C0 judged time-sensitive + a configured template → nudge fires
    client = _FakeClient(
        results=[
            TwilioMessageResult(sid="SM1", status="failed", error_code=63016),  # free-form rejected
            TwilioMessageResult(sid="SM2", status="queued", error_code=None),  # the template send
        ]
    )
    ref = ChannelRef(platform="whatsapp", channel_key="+15551230000")
    conn = _connector(client, ref=ref, template_sid="HX123", time_sensitive=True)
    result = await conn.deliver(_originated("conv_1"))
    assert result.outcome is DeliveryOutcome.FAILED  # the CONTENT still didn't deliver free-form
    assert result.detail == "send-window closed; re-engagement template sent"
    assert len(client.calls) == 2  # free-form attempt + the template nudge
    assert client.calls[1]["content_sid"] == "HX123"


@pytest.mark.asyncio
async def test_deliver_window_closed_time_sensitive_but_no_template_persist_only() -> None:
    client = _FakeClient(
        results=[TwilioMessageResult(sid="SM1", status="failed", error_code=63016)]
    )
    ref = ChannelRef(platform="whatsapp", channel_key="+15551230000")
    conn = _connector(client, ref=ref, template_sid="", time_sensitive=True)  # urgent, no template
    result = await conn.deliver(_originated("conv_1"))
    assert result.detail == WINDOW_CLOSED_DETAIL  # nothing to send → persist-only
    assert len(client.calls) == 1
