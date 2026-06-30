"""PhoneInboundFlow — the shared WhatsApp/SMS orchestrator (Spec C4 T13), unit-level.

Drives raw Twilio inbound form params through the orchestrator with injected fakes,
asserting the surface wiring: ignore → noop; non-text → the platform decline; a valid
texted-back OTP code → bind + confirmation (the auth carrier, the binding WRITE); a
non-code from an UNLINKED number → the link-instruction (delegates to the shared flow,
which resolves → zero access); a non-code from a LINKED number → routes to a turn.

The OTP carrier mirrors Telegram's ``/start`` redeem EXACTLY (just a plain code, no deep
link) — the load-bearing property re-asserted here is that the bound identity is the
**webhook-derived ``sender_id``**, NEVER the message text. Exercised against a FAKE
in-memory LinkStore (no DB) + a fake transport (records the I/O); the routing DECISION is
C1's (tested in test_routing) — here we prove the phone surface wires it correctly.
"""
# ruff: noqa: ARG002 — the fakes mirror real protocol signatures; unused params intentional.

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona_connectors._phone.flow import PhoneInboundFlow
from persona_connectors._phone.linking import PhoneLinkingService
from persona_connectors.domain.conversation_model import ForegroundResult
from persona_connectors.domain.flow import SharedInboundFlow, TurnRequest
from persona_connectors.domain.linking import LinkingService, LinkRecord, LinkToken
from persona_connectors.domain.resolution import InboundIdentityResolver
from persona_connectors.whatsapp.inbound import (
    InboundNonText,
    InboundText,
    NonTextKind,
    classify_inbound,
)
from persona_connectors.whatsapp.non_text import decline_message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona_connectors.domain.normalise import NormalisedOutbound

_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)
_PLATFORM = "whatsapp"
_SENDER = "+15551230000"  # the bare-E.164 identity (whatsapp: prefix stripped on inbound)
_NAMES = {"astrid": ["Astrid"], "kai": ["Kai"]}


class _FakeLinkStore:
    """Minimal in-memory LinkStore (the persistence port faked for pure-logic tests)."""

    def __init__(self) -> None:
        self.tokens: dict[str, LinkToken] = {}
        self.identities: list[LinkRecord] = []

    def create_token(self, token: LinkToken) -> None:
        self.tokens[token.token_hash] = token

    def get_token_by_hash(self, token_hash: str) -> LinkToken | None:
        return self.tokens.get(token_hash)

    def consume_token(self, token_hash: str, *, now: datetime) -> None:
        tok = self.tokens[token_hash]
        self.tokens[token_hash] = tok.model_copy(update={"status": "consumed", "consumed_at": now})

    def bind_identity(
        self, *, platform: str, platform_identity: str, owner_id: str, now: datetime
    ) -> None:
        self.identities.append(
            LinkRecord(
                platform=platform,
                platform_identity=platform_identity,
                owner_id=owner_id,
                status="active",
                linked_at=now,
            )
        )

    def get_active_identity(self, *, platform: str, platform_identity: str) -> LinkRecord | None:
        for rec in self.identities:
            if (
                rec.platform == platform
                and rec.platform_identity == platform_identity
                and rec.status == "active"
            ):
                return rec
        return None

    def revoke_identity(
        self, *, owner_id: str, platform: str, platform_identity: str, now: datetime
    ) -> None:  # pragma: no cover - unused by the flow tests
        for i, rec in enumerate(self.identities):
            if (
                rec.owner_id == owner_id
                and rec.platform == platform
                and rec.platform_identity == platform_identity
                and rec.status == "active"
            ):
                self.identities[i] = rec.model_copy(update={"status": "revoked", "revoked_at": now})


class _FakeTransport:
    """A fake :class:`FlowTransport` recording the system + persona sends (no real I/O)."""

    def __init__(self) -> None:
        self.system: list[tuple[str, str]] = []
        self.personas: list[NormalisedOutbound] = []

    async def send_system(self, *, conversation_key: str, text: str) -> None:
        self.system.append((conversation_key, text))

    async def send_persona(self, outbound: NormalisedOutbound) -> None:
        self.personas.append(outbound)

    def typing(self, conversation_key: str) -> contextlib.AbstractAsyncContextManager[None]:
        return _no_typing()


@contextlib.asynccontextmanager
async def _no_typing() -> AsyncIterator[None]:
    await asyncio.sleep(0)  # yield so a real turn's typing task would fire
    yield


class _FakeStore:
    """A minimal ConversationStateStore — foreground always creates a fresh slot."""

    def __init__(self) -> None:
        self.foregrounded: list[str] = []

    def current_foreground(self, *, owner_id: str, platform: str, channel_key: str) -> None:
        return None

    def foreground(
        self, *, owner_id: str, platform: str, channel_key: str, persona_id: str
    ) -> ForegroundResult:
        self.foregrounded.append(persona_id)
        return ForegroundResult(conversation_id=f"conv_{persona_id}", resumed=False)

    def apply_new(self, *, owner_id: str, platform: str, channel_key: str) -> str | None:
        return "conv_new"


class _TurnRunner:
    def __init__(self, reply: str = "Hello from the persona") -> None:
        self.reply = reply
        self.requests: list[TurnRequest] = []

    async def __call__(self, request: TurnRequest) -> str:
        await asyncio.sleep(0)
        self.requests.append(request)
        return self.reply


def _service() -> tuple[PhoneLinkingService, _FakeLinkStore]:
    store = _FakeLinkStore()
    return PhoneLinkingService(linking=LinkingService(store), platform=_PLATFORM), store


def _flow(
    *,
    linking: PhoneLinkingService,
    turn: _TurnRunner | None = None,
) -> tuple[PhoneInboundFlow[NonTextKind], _FakeTransport, _TurnRunner]:
    """Assemble a PhoneInboundFlow over the carrier's own (fake) link store + fakes.

    The resolver reads the SAME fake link store the carrier binds into, so a bind made via
    a texted code (or pre-seeded) is visible to a subsequent resolve (real read-after-write
    semantics, no DB).
    """
    transport = _FakeTransport()
    turn = turn or _TurnRunner()
    # The resolver reads the SAME fake link store the carrier binds into — so a bind made
    # via a texted code is visible to a subsequent resolve (the real read-after-write).
    resolver = InboundIdentityResolver(linking._linking)  # noqa: SLF001 - test reuses the store
    shared = SharedInboundFlow(
        resolver=resolver,
        conversation_store=_FakeStore(),  # type: ignore[arg-type]
        list_persona_names=lambda _owner: _NAMES,
        run_turn=turn,
    )
    flow = PhoneInboundFlow(
        platform=_PLATFORM,
        classify_inbound=classify_inbound,
        inbound_text_type=InboundText,
        inbound_non_text_type=InboundNonText,
        decline_message=decline_message,
        linking=linking,
        shared=shared,
        transport=transport,
        now=lambda: _NOW,
    )
    return flow, transport, turn


def _params(body: str, *, num_media: int = 0, content_type: str = "") -> dict[str, str]:
    p = {
        "From": f"whatsapp:{_SENDER}",
        "To": "whatsapp:+14155550000",
        "MessageSid": "SM1",
        "Body": body,
        "NumMedia": str(num_media),
    }
    if content_type:
        p["MediaContentType0"] = content_type
    return p


# --- classification paths ---


@pytest.mark.asyncio
async def test_ignore_does_nothing() -> None:
    svc, _ = _service()
    flow, transport, turn = _flow(linking=svc)
    # No MessageSid → InboundIgnore(malformed).
    await flow.handle({"Body": "hi"})
    assert transport.system == []
    assert transport.personas == []
    assert turn.requests == []


@pytest.mark.asyncio
async def test_non_text_sends_a_decline() -> None:
    svc, _ = _service()
    flow, transport, turn = _flow(linking=svc)
    await flow.handle(_params("", num_media=1, content_type="image/png"))
    assert len(transport.system) == 1
    key, text = transport.system[0]
    assert key == _SENDER  # the decline goes back to the sender's conversation
    assert "text" in text.lower()  # the friendly text-only decline
    assert turn.requests == []  # no runtime turn for non-text


# --- the OTP auth carrier (the binding WRITE, surface-side) ---


@pytest.mark.asyncio
async def test_valid_texted_code_binds_and_confirms_without_a_turn() -> None:
    """A texted-back code redeems + binds the WEBHOOK identity, then confirms (no turn)."""
    svc, store = _service()
    code = svc.issue_code(owner_id="user_a", now=_NOW, ttl=timedelta(minutes=10))
    flow, transport, turn = _flow(linking=svc)

    await flow.handle(_params(code))

    # The confirmation was sent as a system message; no persona turn ran.
    assert len(transport.system) == 1
    assert "linked" in transport.system[0][1].lower()
    assert turn.requests == []
    # The bind is to the webhook-derived sender_id (the bare E.164), NEVER the text.
    assert store.get_active_identity(platform=_PLATFORM, platform_identity=_SENDER) is not None
    assert store.get_active_identity(platform=_PLATFORM, platform_identity=code) is None


@pytest.mark.asyncio
async def test_unknown_code_fails_closed_with_retry_copy() -> None:
    svc, _ = _service()
    flow, transport, turn = _flow(linking=svc)
    await flow.handle(_params("ABCD2345"))  # code-shaped, but never issued → failed
    assert len(transport.system) == 1
    assert turn.requests == []  # no turn; no bind


# --- non-code messages → the shared flow (resolve → instruct | route) ---


@pytest.mark.asyncio
async def test_non_code_from_unlinked_number_gets_link_instruction() -> None:
    """An unlinked sender's normal message → the link-instruction, ZERO access (criterion 9)."""
    svc, _ = _service()  # nothing bound
    flow, transport, turn = _flow(linking=svc)
    await flow.handle(_params("Astrid, hello"))
    assert len(transport.system) == 1
    assert "link" in transport.system[0][1].lower()  # the instruction, not a persona reply
    assert transport.personas == []  # no persona reached
    assert turn.requests == []  # never reached the runtime


@pytest.mark.asyncio
async def test_non_code_from_linked_number_routes_to_a_turn() -> None:
    """A LINKED sender addressing a persona → the turn runs + the persona reply is sent."""
    svc, store = _service()
    # Bind the sender first (as a prior redeem would have).
    store.bind_identity(platform=_PLATFORM, platform_identity=_SENDER, owner_id="user_a", now=_NOW)
    flow, transport, turn = _flow(linking=svc)

    await flow.handle(_params("Astrid, hello"))

    assert len(turn.requests) == 1
    assert turn.requests[0].owner_id == "user_a"
    assert turn.requests[0].persona_id == "astrid"
    assert len(transport.personas) == 1
    assert transport.personas[0].text == "Hello from the persona"
    assert transport.system == []  # no link-instruction; the sender is linked


@pytest.mark.asyncio
async def test_linked_sender_texting_a_code_shaped_word_converses_not_code_error() -> None:
    """A LINKED sender's message that happens to be 8 Crockford-base32 chars ("WHATEVER",
    "deadbeef") is conversation, NOT a redeem attempt — the redeem is gated unlinked-only,
    so they reach the shared flow (here the two-persona list), never the confusing
    "code didn't work" reply. The fix for the named Group-F edge."""
    svc, store = _service()
    store.bind_identity(platform=_PLATFORM, platform_identity=_SENDER, owner_id="user_a", now=_NOW)
    flow, transport, turn = _flow(linking=svc)

    await flow.handle(_params("WHATEVER"))  # 8 base32 chars — would have tripped the old redeem

    # NEVER the failed-redeem copy — the linked sender conversed
    assert all("didn't work" not in msg.lower() for _, msg in transport.system)
    # reached the shared flow: two personas + no name → the list-and-instructions reply
    assert len(transport.system) == 1
    assert "Astrid" in transport.system[0][1]
    assert "Kai" in transport.system[0][1]
