"""InboundFlow — the Telegram orchestrator (Spec C2 flow), unit-level with fakes.

Drives raw Telegram updates through the flow with injected fakes, asserting the
routing + command + linking + ownership wiring + the no-streaming typing indicator.
The routing DECISION is C1's (tested in test_routing); here we prove the I/O wires
it correctly: the right persona is foregrounded, the turn is collected, and the
reply is sent — and ownership holds (an unlinked identity gets zero access).

R9-065 additions: the observability decision points (ignore reason, ``/start``
redeem status) actually log, AND — the important guardrail — no emitted log
record ever contains message text or the raw ``/start`` link token.
"""
# ruff: noqa: ARG002 — the fakes mirror real protocol signatures; unused params are intentional.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from loguru import logger as _loguru_logger
from persona.delivery import DeliveryOutcome, DeliveryResult
from persona_connectors.domain.conversation_model import ForegroundRef, ForegroundResult
from persona_connectors.domain.resolution import ResolvedIdentity, UnlinkedIdentity
from persona_connectors.telegram.flow import InboundFlow, TurnRequest
from persona_connectors.telegram.linking import RedeemResult, RedeemStatus
from persona_connectors.telegram.replies import (
    NEW_CONVERSATION_MESSAGE,
    NO_ACTIVE_TO_RESET_MESSAGE,
    NO_PERSONAS_MESSAGE,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)
_CHAT = "555"
_NAMES = {"astrid": ["Astrid"], "kai": ["Kai"]}


@pytest.fixture
def loguru_capture() -> Iterator[list[str]]:
    """Loguru sink capturing every emitted line — ``persona.logging`` wraps loguru,
    so stdlib ``caplog`` sees nothing (the established repo idiom, e.g.
    ``test_api_app_factory.py``'s ``loguru_info_capture``). Captured at the lowest
    level so the privacy guardrail test below sees EVERYTHING this module emits."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="TRACE")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


class _FakeClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self.chat_actions: list[str] = []

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        parse_mode: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> dict[str, object]:
        self.messages.append((chat_id, text))
        return {"message_id": 1}

    async def send_chat_action(self, *, chat_id: str, action: str = "typing") -> None:
        self.chat_actions.append(chat_id)


class _FakeConnector:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, outbound: object) -> DeliveryResult:
        self.sent.append(outbound)
        return DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel="telegram")


class _FakeResolver:
    def __init__(self, result: object) -> None:
        self._result = result

    def resolve(self, inbound: object) -> object:
        return self._result


class _FakeLinking:
    def __init__(self, result: RedeemResult) -> None:
        self._result = result

    def redeem_start_command(
        self, *, text: str, platform_identity: str, now: datetime
    ) -> RedeemResult:
        return self._result


class _FakeStore:
    def __init__(
        self, *, active: ForegroundRef | None = None, apply_new_result: str | None = "conv_new"
    ) -> None:
        self.active = active
        self.apply_new_result = apply_new_result
        self.foregrounded: list[str] = []
        self.applied_new: list[str] = []

    def current_foreground(
        self, *, owner_id: str, platform: str, channel_key: str
    ) -> ForegroundRef | None:
        return self.active

    def foreground(
        self, *, owner_id: str, platform: str, channel_key: str, persona_id: str
    ) -> ForegroundResult:
        self.foregrounded.append(persona_id)
        return ForegroundResult(conversation_id=f"conv_for_{persona_id}", resumed=False)

    def apply_new(self, *, owner_id: str, platform: str, channel_key: str) -> str | None:
        self.applied_new.append(channel_key)
        return self.apply_new_result


class _TurnRunner:
    def __init__(self, reply: str = "Hello from the persona") -> None:
        self.reply = reply
        self.requests: list[TurnRequest] = []

    async def __call__(self, request: TurnRequest) -> str:
        await asyncio.sleep(0)  # yield so the typing refresh task fires once
        self.requests.append(request)
        return self.reply


def _flow(
    *,
    resolver: object = None,
    linking: _FakeLinking | None = None,
    store: _FakeStore | None = None,
    names: dict[str, list[str]] | None = None,
    turn: _TurnRunner | None = None,
    client: _FakeClient | None = None,
    connector: _FakeConnector | None = None,
) -> tuple[InboundFlow, _FakeClient, _FakeConnector, _FakeStore, _TurnRunner]:
    client = client or _FakeClient()
    connector = connector or _FakeConnector()
    store = store or _FakeStore()
    turn = turn or _TurnRunner()
    resolver = resolver or _FakeResolver(ResolvedIdentity(owner_id="user_a"))
    linking = linking or _FakeLinking(RedeemResult(status=RedeemStatus.not_a_link_attempt))
    resolved_names = _NAMES if names is None else names
    flow = InboundFlow(
        resolver=resolver,  # type: ignore[arg-type]
        linking=linking,  # type: ignore[arg-type]
        conversation_store=store,  # type: ignore[arg-type]
        connector=connector,  # type: ignore[arg-type]
        client=client,  # type: ignore[arg-type]
        list_persona_names=lambda _owner: resolved_names,
        run_turn=turn,
        now=lambda: _NOW,
    )
    return flow, client, connector, store, turn


def _text_update(text: str) -> dict[str, object]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 9,
            "from": {"id": 777},
            "chat": {"id": int(_CHAT), "type": "private"},
            "date": int(_NOW.timestamp()),
            "text": text,
        },
    }


# --- classification paths ---


@pytest.mark.asyncio
async def test_ignore_update_does_nothing() -> None:
    flow, client, connector, _store, turn = _flow()
    await flow.handle({"update_id": 1, "edited_message": {"message_id": 5}})
    assert client.messages == []
    assert connector.sent == []
    assert turn.requests == []


@pytest.mark.asyncio
async def test_non_text_sends_a_decline() -> None:
    flow, client, connector, _store, turn = _flow()
    update = {
        "update_id": 1,
        "message": {
            "message_id": 9,
            "from": {"id": 777},
            "chat": {"id": int(_CHAT), "type": "private"},
            "date": int(_NOW.timestamp()),
            "voice": {"file_id": "x"},
        },
    }
    await flow.handle(update)
    assert len(client.messages) == 1
    assert "voice" in client.messages[0][1].lower()
    assert turn.requests == []  # no runtime turn for non-text


# --- linking + ownership ---


@pytest.mark.asyncio
async def test_start_token_linked_confirms_without_a_turn() -> None:
    flow, client, _connector, _store, turn = _flow(
        linking=_FakeLinking(
            RedeemResult(status=RedeemStatus.linked, owner_id="user_a", message="You're linked!")
        )
    )
    await flow.handle(_text_update("/start sometoken"))
    assert client.messages == [(_CHAT, "You're linked!")]
    assert turn.requests == []


@pytest.mark.asyncio
async def test_start_token_failed_sends_retry() -> None:
    flow, client, _connector, _store, _turn = _flow(
        linking=_FakeLinking(RedeemResult(status=RedeemStatus.failed, message="didn't work"))
    )
    await flow.handle(_text_update("/start staletoken"))
    assert client.messages == [(_CHAT, "didn't work")]


@pytest.mark.asyncio
async def test_unlinked_identity_gets_link_instruction_zero_access() -> None:
    """Ownership boundary: an unlinked identity gets the instruction and NO turn."""
    flow, client, connector, _store, turn = _flow(
        resolver=_FakeResolver(UnlinkedIdentity(instruction="link this account"))
    )
    await flow.handle(_text_update("Kai, hello"))
    assert client.messages == [(_CHAT, "link this account")]
    assert connector.sent == []
    assert turn.requests == []  # never reached the runtime


# --- commands ---


@pytest.mark.asyncio
async def test_new_with_active_resets_and_confirms() -> None:
    flow, client, _connector, store, _turn = _flow(store=_FakeStore(apply_new_result="conv_new"))
    await flow.handle(_text_update("/new"))
    assert store.applied_new == [_CHAT]
    assert client.messages == [(_CHAT, NEW_CONVERSATION_MESSAGE)]


@pytest.mark.asyncio
async def test_new_with_no_active_says_nothing_to_reset() -> None:
    flow, client, _connector, _store, _turn = _flow(store=_FakeStore(apply_new_result=None))
    await flow.handle(_text_update("/new"))
    assert client.messages == [(_CHAT, NO_ACTIVE_TO_RESET_MESSAGE)]


@pytest.mark.asyncio
async def test_bare_start_when_linked_lists_personas() -> None:
    flow, client, _connector, _store, turn = _flow()
    await flow.handle(_text_update("/start"))
    assert "Astrid" in client.messages[0][1]
    assert turn.requests == []


@pytest.mark.asyncio
async def test_no_personas_tells_the_user_to_create_one() -> None:
    flow, client, _connector, _store, _turn = _flow(names={})
    await flow.handle(_text_update("hello"))
    assert client.messages == [(_CHAT, NO_PERSONAS_MESSAGE)]


# --- routing → turn → send (with typing) ---


@pytest.mark.asyncio
async def test_addressed_persona_is_foregrounded_and_driven() -> None:
    flow, client, connector, store, turn = _flow()
    await flow.handle(_text_update("Kai, how are you?"))

    assert store.foregrounded == ["kai"]  # the addressed persona was foregrounded
    assert turn.requests[0].persona_id == "kai"
    assert turn.requests[0].conversation_id == "conv_for_kai"
    assert turn.requests[0].text == "Kai, how are you?"
    # The reply was sent with the persona tag + the chat as the conversation_key.
    assert len(connector.sent) == 1
    outbound = connector.sent[0]
    assert outbound.persona.display_name == "Kai"  # type: ignore[attr-defined]
    assert outbound.text == "Hello from the persona"  # type: ignore[attr-defined]
    assert outbound.conversation_key == _CHAT  # type: ignore[attr-defined]
    # The typing indicator fired at least once while the turn ran (D-C2-4).
    assert client.chat_actions == [_CHAT]


@pytest.mark.asyncio
async def test_no_name_continues_the_active_persona() -> None:
    """An unnamed message routes to the active persona (sticky pointer)."""
    store = _FakeStore(active=ForegroundRef(persona_id="astrid", conversation_id="conv_astrid"))
    flow, _client, connector, store, turn = _flow(store=store)
    await flow.handle(_text_update("how are you?"))
    assert store.foregrounded == ["astrid"]
    assert turn.requests[0].persona_id == "astrid"
    assert connector.sent[0].persona.display_name == "Astrid"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_no_name_no_active_multiple_personas_lists() -> None:
    flow, client, connector, _store, turn = _flow(store=_FakeStore(active=None))
    await flow.handle(_text_update("hello there"))
    assert "Astrid" in client.messages[0][1]
    assert connector.sent == []
    assert turn.requests == []


# --- observability (R9-065) ---


@pytest.mark.asyncio
async def test_ignored_update_logs_the_reason(loguru_capture: list[str]) -> None:
    """An InboundIgnore — the most likely silent-drop site — must log its reason."""
    flow, _client, _connector, _store, _turn = _flow()
    await flow.handle({"update_id": 42, "edited_message": {"message_id": 5}})
    assert any("non-message-update" in line for line in loguru_capture), loguru_capture


@pytest.mark.asyncio
async def test_start_redeem_linked_logs_the_status(loguru_capture: list[str]) -> None:
    flow, _client, _connector, _store, _turn = _flow(
        linking=_FakeLinking(
            RedeemResult(status=RedeemStatus.linked, owner_id="user_a", message="You're linked!")
        )
    )
    await flow.handle(_text_update("/start sometoken"))
    assert any("status=linked" in line for line in loguru_capture), loguru_capture


@pytest.mark.asyncio
async def test_start_redeem_failed_logs_the_status(loguru_capture: list[str]) -> None:
    flow, _client, _connector, _store, _turn = _flow(
        linking=_FakeLinking(RedeemResult(status=RedeemStatus.failed, message="didn't work"))
    )
    await flow.handle(_text_update("/start staletoken"))
    assert any("status=failed" in line for line in loguru_capture), loguru_capture


@pytest.mark.asyncio
async def test_not_a_link_attempt_logs_the_status(loguru_capture: list[str]) -> None:
    """A normal message still runs the redeem check (bare-/start branch) — logged too."""
    flow, _client, _connector, _store, _turn = _flow()
    await flow.handle(_text_update("Kai, hello"))
    assert any("status=not_a_link_attempt" in line for line in loguru_capture), loguru_capture


@pytest.mark.asyncio
async def test_no_log_record_ever_contains_message_text_or_the_start_token(
    loguru_capture: list[str],
) -> None:
    """The important guardrail: message content and the /start bearer token must
    NEVER reach a log record, across every branch this module can take — a plain
    message, a linked /start, and a failed /start."""
    sentinel_text = "SENTINEL-MESSAGE-BODY-DO-NOT-LOG-9f3c"
    sentinel_token = "SENTINEL-START-TOKEN-DO-NOT-LOG-7ae1"

    plain_flow, *_ = _flow()
    await plain_flow.handle(_text_update(f"hello {sentinel_text} world"))

    linked_flow, *_ = _flow(
        linking=_FakeLinking(
            RedeemResult(status=RedeemStatus.linked, owner_id="user_a", message="linked!")
        )
    )
    await linked_flow.handle(_text_update(f"/start {sentinel_token}"))

    failed_flow, *_ = _flow(
        linking=_FakeLinking(RedeemResult(status=RedeemStatus.failed, message="nope"))
    )
    await failed_flow.handle(_text_update(f"/start {sentinel_token}-failed"))

    for line in loguru_capture:
        assert sentinel_text not in line, f"message text leaked into a log line: {line!r}"
        assert sentinel_token not in line, f"/start token leaked into a log line: {line!r}"
