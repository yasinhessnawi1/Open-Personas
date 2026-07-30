"""SharedInboundFlow — the platform-agnostic inbound skeleton (Spec C3 amendment #2).

Drives a normalised text inbound through resolve → command → route → turn → send
with injected fakes + a fake :class:`FlowTransport`, asserting the shared sequence
and the auth boundary (the flow only READS a binding via the resolver — it never
redeems/binds; that stays surface-side). The platform deltas (classify, the
``/start``/OAuth carrier, the typing I/O) are NOT here — they live in each adapter.
"""
# ruff: noqa: ARG002 — the fakes mirror real protocol signatures; unused params are intentional.

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.delivery import DeliveryOutcome, DeliveryResult
from persona_connectors.domain.conversation_model import ForegroundRef, ForegroundResult
from persona_connectors.domain.flow import (
    FlowCommands,
    FlowTransport,
    SharedInboundFlow,
    TurnRequest,
)
from persona_connectors.domain.normalise import NormalisedInbound, NormalisedOutbound
from persona_connectors.domain.resolution import ResolvedIdentity, UnlinkedIdentity
from persona_connectors.domain.system_replies import (
    NEW_CONVERSATION_MESSAGE,
    NO_ACTIVE_TO_RESET_MESSAGE,
    NO_PERSONAS_MESSAGE,
    TURN_FAILED_MESSAGE,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)
_CHAT = "chan-1"
_NAMES = {"astrid": ["Astrid"], "kai": ["Kai"]}
# Telegram's greeting word — proves the vocabulary is injected, not hard-coded.
_TELEGRAM_COMMANDS = FlowCommands(greeting_commands=frozenset({"/start"}))


class _FakeTransport:
    """Records the platform I/O the shared flow drives (a FlowTransport)."""

    def __init__(self) -> None:
        self.system: list[tuple[str, str]] = []
        self.persona: list[NormalisedOutbound] = []
        self.typed: list[str] = []

    async def send_system(self, *, conversation_key: str, text: str) -> None:
        self.system.append((conversation_key, text))

    async def send_persona(self, outbound: NormalisedOutbound) -> None:
        self.persona.append(outbound)

    @contextlib.asynccontextmanager
    async def typing(self, conversation_key: str) -> AsyncIterator[None]:
        self.typed.append(conversation_key)
        yield


class _FakeResolver:
    def __init__(self, result: object) -> None:
        self._result = result

    def resolve(self, inbound: object) -> object:
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
        await asyncio.sleep(0)
        self.requests.append(request)
        return self.reply


class _FlakyTurnRunner:
    """Fails on its first N calls (a provider/model/credits fault), then serves normally.

    Stands in for the production shape (R9-073b): a provider raises out of ``run_turn``.
    """

    def __init__(
        self,
        *,
        fail_times: int = 1,
        fail_message: str = "boom",
        reply: str = "Hello from the persona",
    ) -> None:
        self._fail_times = fail_times
        self._fail_message = fail_message
        self.reply = reply
        self.calls = 0

    async def __call__(self, request: TurnRequest) -> str:
        await asyncio.sleep(0)
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError(self._fail_message)
        return self.reply


async def _cancelling_turn(_request: TurnRequest) -> str:
    """A run_turn stand-in for a genuine shutdown mid-turn — never treated as a fault."""
    raise asyncio.CancelledError


def _flow(
    *,
    resolver: object = None,
    store: _FakeStore | None = None,
    names: dict[str, list[str]] | None = None,
    turn: _TurnRunner | None = None,
    commands: FlowCommands = _TELEGRAM_COMMANDS,
) -> tuple[SharedInboundFlow, _FakeTransport, _FakeStore, _TurnRunner]:
    transport = _FakeTransport()
    store = store or _FakeStore()
    turn = turn or _TurnRunner()
    resolver = resolver or _FakeResolver(ResolvedIdentity(owner_id="user_a"))
    resolved_names = _NAMES if names is None else names
    flow = SharedInboundFlow(
        resolver=resolver,  # type: ignore[arg-type]
        conversation_store=store,  # type: ignore[arg-type]
        list_persona_names=lambda _owner: resolved_names,
        run_turn=turn,
        commands=commands,
    )
    return flow, transport, store, turn


def _inbound(text: str) -> NormalisedInbound:
    return NormalisedInbound(
        platform="telegram",
        sender_id="777",
        conversation_key=_CHAT,
        message_id="9",
        text=text,
        received_at=_NOW,
    )


# --- the FlowTransport / FlowCommands contracts ---


def test_fake_transport_satisfies_the_protocol() -> None:
    assert isinstance(_FakeTransport(), FlowTransport)


def test_flow_commands_is_frozen_with_defaults() -> None:
    cmd = FlowCommands()
    assert cmd.new_command == "/new"
    assert cmd.greeting_commands == frozenset()
    with pytest.raises(ValueError, match="frozen"):
        cmd.new_command = "/x"  # type: ignore[misc]


# --- ownership boundary ---


@pytest.mark.asyncio
async def test_unlinked_identity_gets_link_instruction_zero_access() -> None:
    """An unlinked identity gets the instruction and NEVER reaches a turn (zero access)."""
    flow, transport, _store, turn = _flow(
        resolver=_FakeResolver(UnlinkedIdentity(instruction="link this account"))
    )
    await flow.handle_text(_inbound("Kai, hello"), transport=transport)
    assert transport.system == [(_CHAT, "link this account")]
    assert transport.persona == []
    assert turn.requests == []


@pytest.mark.asyncio
async def test_no_personas_tells_the_user_to_create_one() -> None:
    flow, transport, _store, _turn = _flow(names={})
    await flow.handle_text(_inbound("hello"), transport=transport)
    assert transport.system == [(_CHAT, NO_PERSONAS_MESSAGE)]


# --- commands ---


@pytest.mark.asyncio
async def test_new_with_active_resets_and_confirms() -> None:
    flow, transport, store, _turn = _flow(store=_FakeStore(apply_new_result="conv_new"))
    await flow.handle_text(_inbound("/new"), transport=transport)
    assert store.applied_new == [_CHAT]
    assert transport.system == [(_CHAT, NEW_CONVERSATION_MESSAGE)]


@pytest.mark.asyncio
async def test_new_with_no_active_says_nothing_to_reset() -> None:
    flow, transport, _store, _turn = _flow(store=_FakeStore(apply_new_result=None))
    await flow.handle_text(_inbound("/new"), transport=transport)
    assert transport.system == [(_CHAT, NO_ACTIVE_TO_RESET_MESSAGE)]


@pytest.mark.asyncio
async def test_greeting_command_lists_personas() -> None:
    """The injected greeting word (Telegram's /start) replies with the list, no turn."""
    flow, transport, _store, turn = _flow()
    await flow.handle_text(_inbound("/start"), transport=transport)
    assert "Astrid" in transport.system[0][1]
    assert turn.requests == []


@pytest.mark.asyncio
async def test_unknown_greeting_word_is_not_a_command() -> None:
    """With no greeting vocabulary, /start is just text → routed, not greeted."""
    flow, transport, _store, turn = _flow(commands=FlowCommands())
    await flow.handle_text(_inbound("/start"), transport=transport)
    # No greeting configured + no active persona + 2 personas → list-and-instructions
    # via the ROUTER (not the command branch); still no turn.
    assert "Astrid" in transport.system[0][1]
    assert turn.requests == []


# --- routing → turn → send ---


@pytest.mark.asyncio
async def test_addressed_persona_is_foregrounded_and_driven() -> None:
    flow, transport, store, turn = _flow()
    await flow.handle_text(_inbound("Kai, how are you?"), transport=transport)

    assert store.foregrounded == ["kai"]
    assert turn.requests[0].persona_id == "kai"
    assert turn.requests[0].conversation_id == "conv_for_kai"
    assert turn.requests[0].text == "Kai, how are you?"
    assert len(transport.persona) == 1
    assert transport.persona[0].persona.display_name == "Kai"
    assert transport.persona[0].text == "Hello from the persona"
    assert transport.persona[0].conversation_key == _CHAT
    assert transport.typed == [_CHAT]  # the typing window opened around the turn


@pytest.mark.asyncio
async def test_no_name_continues_the_active_persona() -> None:
    store = _FakeStore(active=ForegroundRef(persona_id="astrid", conversation_id="conv_astrid"))
    flow, transport, store, turn = _flow(store=store)
    await flow.handle_text(_inbound("how are you?"), transport=transport)
    assert store.foregrounded == ["astrid"]
    assert turn.requests[0].persona_id == "astrid"
    assert transport.persona[0].persona.display_name == "Astrid"


@pytest.mark.asyncio
async def test_no_name_no_active_multiple_personas_lists() -> None:
    flow, transport, _store, turn = _flow(store=_FakeStore(active=None))
    await flow.handle_text(_inbound("hello there"), transport=transport)
    assert "Astrid" in transport.system[0][1]
    assert transport.persona == []
    assert turn.requests == []


def test_delivery_result_helper_imported() -> None:
    """Guard: the fake connector's outcome type stays importable (paranoia)."""
    assert DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel="x").channel == "x"


# --- Spec C5 A2: the envelope-persona seam (plus-addressing) ---


def _owner_flow(
    *, owner_id: str, names_by_owner: dict[str, dict[str, list[str]]]
) -> tuple[SharedInboundFlow, _FakeStore, _TurnRunner]:
    store = _FakeStore()
    turn = _TurnRunner()
    flow = SharedInboundFlow(
        resolver=_FakeResolver(ResolvedIdentity(owner_id=owner_id)),
        conversation_store=store,  # type: ignore[arg-type]
        list_persona_names=lambda owner: names_by_owner[owner],
        run_turn=turn,
        commands=_TELEGRAM_COMMANDS,
    )
    return flow, store, turn


@pytest.mark.asyncio
async def test_envelope_tag_routes_within_the_addressed_owner_only() -> None:
    """Spec C5 D-C5-3 — the RLS security proof, NON-VACUOUS: two owners each have an
    'Astrid' under DIFFERENT persona ids; the envelope tag resolves ONLY within the
    RESOLVED owner's loaded names, so owner A's inbound reaches A's Astrid and owner B's
    reaches B's — never a cross-owner bind to the wrong persona named the same."""
    names_by_owner = {
        "user_a": {"astrid_a": ["Astrid"], "kai_a": ["Kai"]},
        "user_b": {"astrid_b": ["Astrid"]},
    }
    flow_a, store_a, turn_a = _owner_flow(owner_id="user_a", names_by_owner=names_by_owner)
    await flow_a.handle_text(
        _inbound("hello"), transport=_FakeTransport(), envelope_persona_tag="astrid"
    )
    assert store_a.foregrounded == ["astrid_a"]  # A's Astrid — never B's
    assert turn_a.requests[0].persona_id == "astrid_a"

    flow_b, store_b, turn_b = _owner_flow(owner_id="user_b", names_by_owner=names_by_owner)
    await flow_b.handle_text(
        _inbound("hello"), transport=_FakeTransport(), envelope_persona_tag="astrid"
    )
    assert store_b.foregrounded == ["astrid_b"]  # the same tag, B's persona
    assert turn_b.requests[0].persona_id == "astrid_b"


@pytest.mark.asyncio
async def test_envelope_tag_overrides_text_parse() -> None:
    """A unique envelope tag selects the persona even when the body names another —
    the deterministic envelope wins over prose (plus-addressing primary, D-C5-3)."""
    flow, transport, store, turn = _flow()
    await flow.handle_text(
        _inbound("Kai, hello"), transport=transport, envelope_persona_tag="astrid"
    )
    assert store.foregrounded == ["astrid"]  # the envelope tag, not the "Kai" in the text
    assert turn.requests[0].persona_id == "astrid"


@pytest.mark.asyncio
async def test_unknown_envelope_tag_falls_back_to_text_parse() -> None:
    """An unresolvable tag falls back to text-parse — no silent misroute."""
    flow, transport, store, turn = _flow()
    await flow.handle_text(
        _inbound("Kai, hello"), transport=transport, envelope_persona_tag="ghost"
    )
    assert store.foregrounded == ["kai"]  # fell back to the "Kai" in the text


@pytest.mark.asyncio
async def test_none_envelope_tag_is_todays_chat_behavior() -> None:
    """None tag (every non-email adapter passes None / omits it) → the existing
    text-parse routing, byte-for-byte unchanged (the additive guarantee)."""
    flow, transport, store, turn = _flow()
    await flow.handle_text(_inbound("Kai, hello"), transport=transport, envelope_persona_tag=None)
    assert store.foregrounded == ["kai"]
    assert turn.requests[0].persona_id == "kai"


# --- R9-073b: a turn/provider failure must never kill the platform loop ---


@pytest.mark.asyncio
async def test_turn_failure_replies_honestly_and_the_loop_continues() -> None:
    """The production failure (captured 2026-07-29): a provider raised out of ``run_turn``
    (Cloudflare rejecting a model the account's plan can't serve) and that exception
    escaped ``handle_text`` — permanently ending the platform's inbound loop. Now: the
    user gets the honest apology, no exception escapes, and the VERY NEXT message on the
    same flow is still processed normally — proving the loop was never killed.
    """
    turn = _FlakyTurnRunner(
        fail_times=1,
        fail_message=(
            "Model @cf/zai-org/glm-5.2 is not available on the Workers Free plan: "
            "This model requires a Workers Paid plan"
        ),
    )
    flow, transport, _store, _turn = _flow(turn=turn)  # type: ignore[arg-type]

    # First message: the turn blows up.
    await flow.handle_text(_inbound("Kai, hello"), transport=transport)
    assert transport.system == [(_CHAT, TURN_FAILED_MESSAGE)]
    assert transport.persona == []  # no persona reply — a persona never said this

    # Second message on the SAME flow instance: the loop kept going.
    await flow.handle_text(_inbound("Kai, are you there?"), transport=transport)
    assert len(transport.persona) == 1
    assert transport.persona[0].text == "Hello from the persona"
    assert turn.calls == 2


@pytest.mark.asyncio
async def test_turn_failure_reply_never_leaks_provider_or_model_text() -> None:
    """The privacy/leak guard: the error reply is a FIXED product-voice string, never
    built from the provider's own exception text, so a provider name / model id /
    billing detail can never reach the user — mirrors the existing no-content-logging
    posture proven for message bodies (see telegram/test_flow.py)."""
    sentinel_provider = "cloudflare"
    sentinel_model = "@cf/zai-org/glm-5.2"
    turn = _FlakyTurnRunner(
        fail_times=1,
        fail_message=(
            f"AiError: Model {sentinel_model} is not available on the Workers Free plan "
            f"[provider={sentinel_provider} underlying=PermissionDeniedError]"
        ),
    )
    flow, transport, _store, _turn = _flow(turn=turn)  # type: ignore[arg-type]

    await flow.handle_text(_inbound("Kai, hello"), transport=transport)

    assert len(transport.system) == 1
    sent_text = transport.system[0][1]
    assert sent_text == TURN_FAILED_MESSAGE
    assert sentinel_provider not in sent_text
    assert sentinel_model not in sent_text
    assert "PermissionDeniedError" not in sent_text
    assert "cf/zai-org" not in sent_text


@pytest.mark.asyncio
async def test_turn_failure_is_caught_regardless_of_exception_type() -> None:
    """Not just a provider error — ANY exception while producing/sending the reply (a
    store fault, a timeout, ...) must be caught the same way. A plain non-PersonaError
    exception (no ``context``/``redact``-friendly shape) proves the guard doesn't assume
    a particular exception hierarchy."""
    flow, transport, _store, _turn = _flow(
        turn=_FlakyTurnRunner(fail_times=1, fail_message="connection timed out")  # type: ignore[arg-type]
    )
    await flow.handle_text(_inbound("Kai, hello"), transport=transport)
    assert transport.system == [(_CHAT, TURN_FAILED_MESSAGE)]


@pytest.mark.asyncio
async def test_turn_cancelled_error_propagates_not_swallowed() -> None:
    """A clean shutdown (``asyncio.CancelledError``) is a ``BaseException``, not caught by
    the ``except Exception`` turn-failure guard — it must propagate untouched, and the
    honest-apology path must NOT fire for a deliberate cancellation."""
    flow, transport, _store, _turn = _flow(turn=_cancelling_turn)  # type: ignore[arg-type]
    with pytest.raises(asyncio.CancelledError):
        await flow.handle_text(_inbound("Kai, hello"), transport=transport)
    assert transport.system == []
    assert transport.persona == []
