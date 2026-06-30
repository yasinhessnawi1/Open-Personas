"""SmsConnector — C1 Connector + C0 MessageDeliverer (Spec C4 T12).

SMS is the floor: a single send (Twilio auto-concatenates segments), bare-E.164
addresses (no ``whatsapp:`` prefix), the plain-prefix render + segment cap. The
load-bearing SMS truth: **no 24h window** — a C0 originated SMS sends regardless of
when the user last messaged (criterion 5), with NO window-gating leaking over from the
WhatsApp path. Never-silent mapping (delivered/pending/failed) via the shared seam.
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
from persona_connectors.sms.connector import SmsConnector

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

_PERSONA = PersonaIdentityTag(persona_id="p1", display_name="Astrid", visual_ref=None)


class _FakeClient:
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
        status_callback: str | None = None,
    ) -> TwilioMessageResult:
        self.calls.append(
            {"to": to, "from_": from_, "body": body, "status_callback": status_callback}
        )
        if self._raises is not None:
            raise self._raises
        if self._results:
            return self._results.pop(0)
        return TwilioMessageResult(sid="SM1", status="queued", error_code=None)


class _FakeStore:
    def __init__(self, ref: ChannelRef | None) -> None:
        self._ref = ref
        self.seen_conversation_id: str | None = None

    def resolve_channel(self, *, conversation_id: str) -> ChannelRef | None:
        self.seen_conversation_id = conversation_id
        return self._ref


def _scope(entered: list[str]) -> object:
    @contextlib.contextmanager
    def scope(owner_id: str) -> Iterator[None]:
        entered.append(owner_id)
        yield

    return scope


def _connector(
    client: _FakeClient, *, ref: ChannelRef | None = None, entered: list[str] | None = None
) -> SmsConnector:
    return SmsConnector(
        client=client,  # type: ignore[arg-type]
        from_address="+15550000000",
        conversation_store=_FakeStore(ref),  # type: ignore[arg-type]
        owner_scope=_scope(entered if entered is not None else []),  # type: ignore[arg-type]
    )


def _outbound(text: str = "Hello", key: str = "+15551230000") -> NormalisedOutbound:
    return NormalisedOutbound(
        persona=_PERSONA, text=text, conversation_key=key, reply_to_message_id=None
    )


def _originated(conversation_id: str | None) -> OriginatedMessage:
    return OriginatedMessage(
        persona=_PERSONA,
        owner_user_id="user_a",
        content="Reminder: stand-up at 10.",
        conversation_id=conversation_id,
        created_at=datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC),
    )


def test_connector_satisfies_both_protocols() -> None:
    connector = _connector(_FakeClient())
    assert isinstance(connector, Connector)
    assert isinstance(connector, MessageDeliverer)
    assert connector.platform == "sms"
    assert connector.capabilities.can_initiate_freely is True  # no window


# --- send (plain-prefix, bare E.164, single message) ---


@pytest.mark.asyncio
async def test_send_plain_prefix_to_bare_e164() -> None:
    client = _FakeClient()
    result = await _connector(client).send(_outbound("Hi"))
    assert result.outcome is DeliveryOutcome.DELIVERED
    assert client.calls[0]["to"] == "+15551230000"  # bare E.164, NO whatsapp: prefix
    assert client.calls[0]["from_"] == "+15550000000"
    assert client.calls[0]["body"] == "Astrid: Hi"
    assert len(client.calls) == 1  # one send (Twilio auto-concatenates segments)


@pytest.mark.asyncio
async def test_rate_limit_is_pending_api_error_is_failed() -> None:
    pending = await _connector(_FakeClient(raises=TwilioRateLimitError("429", context={}))).send(
        _outbound()
    )
    assert pending.outcome is DeliveryOutcome.PENDING
    failed = await _connector(_FakeClient(raises=TwilioApiError("x", context={}))).send(_outbound())
    assert failed.outcome is DeliveryOutcome.FAILED


@pytest.mark.asyncio
async def test_carrier_error_code_is_failed_never_a_window_detail() -> None:
    # a generic SMS carrier failure → plain failed; SMS has no window, so NEVER the window detail
    client = _FakeClient(
        results=[TwilioMessageResult(sid="SM1", status="failed", error_code=30007)]
    )
    result = await _connector(client).send(_outbound())
    assert result.outcome is DeliveryOutcome.FAILED
    assert result.detail != WINDOW_CLOSED_DETAIL
    assert "30007" in (result.detail or "")


@pytest.mark.asyncio
async def test_long_reply_is_capped_to_a_single_send() -> None:
    client = _FakeClient()
    await _connector(client).send(_outbound("word " * 500))
    assert len(client.calls) == 1  # one message, capped + truncated (not many sends)
    assert (client.calls[0]["body"] or "").endswith("...")


# --- deliver: the SMS no-window C0 truth (criterion 5) ---


@pytest.mark.asyncio
async def test_c0_originated_sms_sends_outside_any_window() -> None:
    # SMS has NO 24h window: a C0 originated message sends regardless of when the user
    # last messaged — no window gate, no template path (criterion 5).
    client = _FakeClient()
    entered: list[str] = []
    ref = ChannelRef(platform="sms", channel_key="+15551230000")
    result = await _connector(client, ref=ref, entered=entered).deliver(_originated("conv_1"))
    assert result.outcome is DeliveryOutcome.DELIVERED  # sent freely
    assert result.detail != WINDOW_CLOSED_DETAIL  # no window gating leaked from WhatsApp
    assert entered == ["user_a"]
    assert client.calls[0]["to"] == "+15551230000"
    assert client.calls[0]["body"] == "Astrid: Reminder: stand-up at 10."


@pytest.mark.asyncio
async def test_deliver_no_channel_is_pending_never_lost() -> None:
    result = await _connector(_FakeClient(), ref=None).deliver(_originated("conv_x"))
    assert result.outcome is DeliveryOutcome.PENDING
    assert "no connector channel" in (result.detail or "")


@pytest.mark.asyncio
async def test_deliver_wrong_platform_is_pending() -> None:
    ref = ChannelRef(platform="whatsapp", channel_key="+1")
    result = await _connector(_FakeClient(), ref=ref).deliver(_originated("conv_y"))
    assert result.outcome is DeliveryOutcome.PENDING


@pytest.mark.asyncio
async def test_deliver_no_conversation_id_is_pending() -> None:
    result = await _connector(_FakeClient()).deliver(_originated(None))
    assert result.outcome is DeliveryOutcome.PENDING
