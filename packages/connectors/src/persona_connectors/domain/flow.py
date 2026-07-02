"""The shared inbound-flow skeleton (Spec C3 amendment #2, D-C3-X-flow-skeleton).

The platform-agnostic inbound sequence every text adapter runs — lifted from the
C2 Telegram orchestrator (``telegram/flow.py``) into the framework's owned surface
so Telegram / Discord / Slack share ONE flow and reimplement only their transport
I/O + their auth carrier. The third platform makes the thin-adapter promise literal
(criterion 10): once C1 owns the flow, each adapter is glue.

**The boundary — where surface-specific auth stays in the surface:**

- SHARED (here, :class:`SharedInboundFlow.handle_text`): resolve an *arrived*
  identity → its owner (or a link-instruction + zero access); load the owner's
  personas; the ``/new`` boundary; a greeting → list-and-instructions; route
  (:func:`~persona_connectors.domain.routing.decide_route` over
  :func:`~persona_connectors.domain.addressing.parse_addressed_persona` +
  :meth:`~persona_connectors.domain.conversation_model.ConversationStateStore.current_foreground`);
  drive the turn (foreground + the no-streaming typing window + ``run_turn`` + send).
  **The shared flow READS bindings (resolve), it NEVER creates them.**
- SURFACE-SIDE (each adapter, around/behind this): inbound ``classify`` + the
  DM-only enforcement; the non-text decline; **all linking/auth carrier** —
  Telegram's ``/start <token>`` deep-link redeem (prepended before delegating) and
  Discord/Slack's OAuth redeem (entirely out-of-band on the callback route, never in
  this flow); and the platform I/O behind :class:`FlowTransport` (``send_system`` /
  ``send_persona`` / ``typing``).

So no token / OAuth / bind concept ever crosses into this module — the auth *write*
is the surface's, the identity *read* is the framework's. The command vocabulary
(:class:`FlowCommands`) is injected, so a surface declares its own greeting word
(Telegram ``/start``; Discord/Slack none) without the flow hard-coding one platform's
convention. Owned surface — api-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.schema.origination import PersonaIdentityTag
from pydantic import BaseModel, ConfigDict, Field

from persona_connectors.domain.addressing import (
    Addressed,
    AddressingResult,
    NoName,
    parse_addressed_persona,
    resolve_persona_tag,
)
from persona_connectors.domain.normalise import NormalisedOutbound
from persona_connectors.domain.resolution import UnlinkedIdentity
from persona_connectors.domain.routing import ListAndInstructions, decide_route
from persona_connectors.domain.system_replies import (
    NEW_CONVERSATION_MESSAGE,
    NO_ACTIVE_TO_RESET_MESSAGE,
    NO_PERSONAS_MESSAGE,
    render_list_and_instructions,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractAsyncContextManager

    from persona_connectors.domain.conversation_model import ConversationStateStore
    from persona_connectors.domain.normalise import NormalisedInbound
    from persona_connectors.domain.resolution import InboundIdentityResolver

__all__ = [
    "FlowCommands",
    "FlowTransport",
    "SharedInboundFlow",
    "TurnRequest",
]


class TurnRequest(BaseModel):
    """What the injected turn-runner needs to drive one persona turn (collected whole)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str
    conversation_id: str
    persona_id: str
    text: str


class FlowCommands(BaseModel):
    """The leading text commands the shared flow recognises — injected per surface.

    ``/new`` (the C1-D-3 conversation boundary) is universal; the greeting commands
    differ per platform (Telegram's bare ``/start``; Discord/Slack have none in v1).
    Keeping the vocabulary as injected data means the shared flow never hard-codes one
    platform's convention.

    Attributes:
        new_command: The leading token that resets the active conversation (``/new``).
        greeting_commands: Leading tokens that reply with the list-and-instructions
            (a manual "who can I talk to?" — empty by default).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_command: str = "/new"
    greeting_commands: frozenset[str] = Field(default_factory=frozenset)


@runtime_checkable
class FlowTransport(Protocol):
    """The per-platform I/O the shared flow needs — **no auth** (see the module docstring).

    Each adapter implements this over its client/connector: ``send_system`` for plain
    system replies (link-instruction, no-personas, ``/new`` confirmations, the list),
    ``send_persona`` for the rendered persona reply (= the connector's ``send``), and
    ``typing`` for the no-streaming working indicator (a no-op where the platform has
    none, e.g. Slack DMs). ``@runtime_checkable`` so a composition root can assert the
    injected transport satisfies the port.
    """

    async def send_system(self, *, conversation_key: str, text: str) -> None:
        """Send a plain-text system reply (the bot speaking — no persona tag)."""
        ...

    async def send_persona(self, outbound: NormalisedOutbound) -> None:
        """Send a rendered persona reply (the connector lowers the name tag + splits)."""
        ...

    def typing(self, conversation_key: str) -> AbstractAsyncContextManager[None]:
        """Show a working indicator while the body runs (a no-op where unsupported)."""
        ...


def _parse_command(text: str, commands: FlowCommands) -> str | None:
    """The leading recognised command (``@bot`` suffix stripped), else ``None``.

    Stripping an ``@bot`` mention suffix is a harmless normalisation everywhere (a
    Discord/Slack command carries no ``@`` to strip); it matches Telegram's
    ``/new@botname`` form. Returns the command only if it is the ``new_command`` or a
    known greeting — a normal message returns ``None``.
    """
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    base = parts[0].split("@", 1)[0]
    if base == commands.new_command or base in commands.greeting_commands:
        return base
    return None


class SharedInboundFlow:
    """Drives one *resolved-or-resolvable* text inbound through the C1 framework.

    All dependencies are injected (DI; no globals). The api-coupled callables
    (``run_turn`` drives ``ConversationLoop.turn``; ``list_persona_names`` reads the
    owner's personas) are owner-scoped by the composition root, keeping this module
    api-free. The surface calls :meth:`handle_text` only after its own
    classification + any auth-carrier step (Telegram's ``/start`` redeem; Discord/Slack
    do their OAuth bind out-of-band), so this flow only ever READS the binding.
    """

    def __init__(
        self,
        *,
        resolver: InboundIdentityResolver,
        conversation_store: ConversationStateStore,
        list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
        run_turn: Callable[[TurnRequest], Awaitable[str]],
        commands: FlowCommands | None = None,
    ) -> None:
        self._resolver = resolver
        self._store = conversation_store
        self._list_persona_names = list_persona_names
        self._run_turn = run_turn
        self._commands = commands if commands is not None else FlowCommands()

    async def handle_text(
        self,
        inbound: NormalisedInbound,
        *,
        transport: FlowTransport,
        envelope_persona_tag: str | None = None,
    ) -> None:
        """Run a normalised text inbound through resolve → route → turn → send.

        Ownership holds exactly as on the web: an unlinked identity gets a
        link-instruction and ZERO access (the C1 resolution gate); a resolved owner
        only ever touches their own personas (the injected callables + the store run
        RLS-scoped to that owner). ``inbound.platform`` keys the store (never branched
        on — D-08-3); the transport owns presentation.

        ``envelope_persona_tag`` (Spec C5, D-C5-3) lets a platform that selects the
        persona in the **envelope** (email plus-addressing — the ESP's ``MailboxHash``)
        supply that selection: a UNIQUE, owner-scoped tag resolution wins over the text
        parse; anything else (no tag / unknown / ambiguous) falls back to parsing the
        body. ``None`` (every non-email adapter) is byte-for-byte today's routing.
        """
        platform = inbound.platform
        chat = inbound.conversation_key

        # 1. Ownership gate (C1): resolve the identity to its owner, or zero access.
        resolution = self._resolver.resolve(inbound)
        if isinstance(resolution, UnlinkedIdentity):
            await transport.send_system(conversation_key=chat, text=resolution.instruction)
            return
        owner_id = resolution.owner_id

        # 2. The owner's personas (RLS-scoped read via the injected lister).
        names = self._list_persona_names(owner_id)
        if not names:
            await transport.send_system(conversation_key=chat, text=NO_PERSONAS_MESSAGE)
            return

        # 3. Boundary / greeting commands (injected vocabulary).
        command = _parse_command(inbound.text, self._commands)
        if command == self._commands.new_command:
            new_conversation = self._store.apply_new(
                owner_id=owner_id, platform=platform, channel_key=chat
            )
            message = (
                NEW_CONVERSATION_MESSAGE
                if new_conversation is not None
                else NO_ACTIVE_TO_RESET_MESSAGE
            )
            await transport.send_system(conversation_key=chat, text=message)
            return
        if command is not None:  # a greeting command → list-and-instructions
            await transport.send_system(
                conversation_key=chat, text=render_list_and_instructions(names)
            )
            return

        # 4. Route (C1's decision) → drive a persona, or list-and-instructions.
        #    A unique envelope tag (D-C5-3, email plus-address) selects the persona
        #    deterministically; else fall back to parsing the body. Resolution is
        #    owner-scoped — `names` is this owner's personas only (never cross-owner).
        addressing: AddressingResult = NoName()
        if envelope_persona_tag is not None:
            tag_result = resolve_persona_tag(envelope_persona_tag, persona_names=names)
            if isinstance(tag_result, Addressed):
                addressing = tag_result
        if isinstance(addressing, NoName):
            addressing = parse_addressed_persona(inbound.text, persona_names=names)
        active = self._store.current_foreground(
            owner_id=owner_id, platform=platform, channel_key=chat
        )
        decision = decide_route(
            addressing,
            active_persona_id=active.persona_id if active is not None else None,
            owner_persona_ids=list(names),
        )
        if isinstance(decision, ListAndInstructions):
            await transport.send_system(
                conversation_key=chat, text=render_list_and_instructions(names)
            )
            return

        # 5. Drive the turn: foreground (flip-or-continue) → collect the reply with a
        #    typing indicator (no-streaming) → render + send whole.
        foreground = self._store.foreground(
            owner_id=owner_id, platform=platform, channel_key=chat, persona_id=decision.persona_id
        )
        addressable = names.get(decision.persona_id)
        display_name = addressable[0] if addressable else decision.persona_id
        tag = PersonaIdentityTag(
            persona_id=decision.persona_id, display_name=display_name, visual_ref=None
        )
        async with transport.typing(chat):
            reply = await self._run_turn(
                TurnRequest(
                    owner_id=owner_id,
                    conversation_id=foreground.conversation_id,
                    persona_id=decision.persona_id,
                    text=inbound.text,
                )
            )
        await transport.send_persona(
            NormalisedOutbound(persona=tag, text=reply, conversation_key=chat)
        )
