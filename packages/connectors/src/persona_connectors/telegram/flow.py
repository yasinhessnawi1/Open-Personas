"""The Telegram inbound flow orchestrator (Spec C2 flow) — the surface I/O + auth carrier.

This wires one inbound Telegram update through the framework: classify → (non-text
decline | ``/start`` deep-link redeem | delegate to the shared flow). The
platform-agnostic sequence (resolve → ``/new`` → route → drive the turn → send) is
C1's — :class:`~persona_connectors.domain.flow.SharedInboundFlow` (D-C3-X-flow-skeleton),
which Telegram / Discord / Slack share. This module supplies only the **Telegram
surface**: the inbound classification, the non-text decline, the **auth carrier**
(the ``/start <token>`` deep-link redeem — the one binding *write*, which stays
surface-side; the shared flow only ever *reads* the binding), and the platform I/O
behind :class:`~persona_connectors.domain.flow.FlowTransport` (the no-streaming
typing loop, the system/persona sends).

The no-streaming pattern (§3, D-C2-4): the reply is collected to completion by the
injected ``run_turn`` while a "typing…" chat action refreshes, then rendered (HTML
bold tag + UTF-16 split) and sent whole — the typing window is opened by the
transport's :meth:`typing` around the shared flow's turn.

**api-free** (the reversibility ideal): the api-coupled bits — running the turn and
listing the owner's personas — are injected callables the composition root supplies;
this module imports no ``persona_api``. Ownership holds exactly as on the web (the
shared C1 resolution gate): an unlinked identity gets a link-instruction and ZERO
access; a resolved owner only ever touches their own personas.

**Observability (R9-065):** every decision point on the inbound path logs — the raw
update's arrival, an :class:`~persona_connectors.telegram.inbound.InboundIgnore`
reason (the most likely silent-drop site), the ``/start`` redeem's
:class:`~persona_connectors.telegram.linking.RedeemStatus`, a non-text decline, and
each system reply this module sends — so a dropped update is traceable end-to-end.
**Never logged:** message text/content (only presence/kind), and never the
``/start`` deep-link token (a bearer credential — only its redeem STATUS is
logged). Telegram chat/user ids are logged (operationally necessary, already
stored).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from persona.logging import get_logger

from persona_connectors.domain.flow import FlowCommands, SharedInboundFlow, TurnRequest
from persona_connectors.telegram.inbound import (
    InboundIgnore,
    InboundNonText,
    classify_update,
)
from persona_connectors.telegram.linking import RedeemStatus
from persona_connectors.telegram.non_text import decline_message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from datetime import datetime

    from persona_connectors.domain.conversation_model import ConversationStateStore
    from persona_connectors.domain.normalise import NormalisedInbound, NormalisedOutbound
    from persona_connectors.domain.resolution import InboundIdentityResolver
    from persona_connectors.telegram.client import TelegramClient
    from persona_connectors.telegram.connector import TelegramConnector
    from persona_connectors.telegram.linking import TelegramLinkingService

# ``TurnRequest`` is the shared turn-runner contract (now in domain.flow); re-exported
# here so existing importers (``telegram/__init__``, the composition root) are unchanged.
__all__ = ["InboundFlow", "TurnRequest"]

_log = get_logger("connectors.telegram_flow")

# The typing chat action lasts ~5s, so refresh just under that while the turn runs.
_TYPING_REFRESH_SECONDS = 4.0

# Telegram's bare ``/start`` greets with the list-and-instructions (the bot convention);
# ``/new`` is the universal C1-D-3 boundary. The shared flow takes this as data so it
# never hard-codes Telegram's verb (D-C3-X-flow-skeleton).
_TELEGRAM_COMMANDS = FlowCommands(greeting_commands=frozenset({"/start"}))


def _peek_conversation_key(update: dict[str, object]) -> str | None:
    """Best-effort chat id straight off the raw update, for logging only.

    Read *before* :func:`~persona_connectors.telegram.inbound.classify_update` runs
    (an ignored/malformed update never reaches a parsed shape), so entry logging can
    still carry the chat id when one is present. Touches only the numeric/string
    ``message.chat.id`` field — never message text/content.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    if isinstance(chat_id, bool):
        return None
    if isinstance(chat_id, int | str) and chat_id != "":
        return str(chat_id)
    return None


@contextlib.asynccontextmanager
async def _typing_indicator(client: TelegramClient, chat_id: str) -> AsyncIterator[None]:
    """Show + refresh the "typing…" chat action while the body runs (D-C2-4).

    Sends the action immediately (instant responsiveness) and re-sends every ~4s
    (the status auto-clears at ~5s) until the context exits; the final reply send
    clears it. The refresh task is always cancelled on exit.
    """

    async def _refresh() -> None:
        while True:
            await client.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)

    task = asyncio.create_task(_refresh())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class _TelegramFlowTransport:
    """Telegram's :class:`~persona_connectors.domain.flow.FlowTransport` — pure I/O, no auth.

    Lowers the shared flow's three I/O needs to Telegram: a plain system send, the
    persona reply via the connector (HTML render + UTF-16 split), and the typing
    working indicator (D-C2-4).
    """

    def __init__(self, *, client: TelegramClient, connector: TelegramConnector) -> None:
        self._client = client
        self._connector = connector

    async def send_system(self, *, conversation_key: str, text: str) -> None:
        await self._client.send_message(chat_id=conversation_key, text=text)

    async def send_persona(self, outbound: NormalisedOutbound) -> None:
        await self._connector.send(outbound)

    def typing(self, conversation_key: str) -> contextlib.AbstractAsyncContextManager[None]:
        return _typing_indicator(self._client, conversation_key)


class InboundFlow:
    """Orchestrates one inbound Telegram update — the Telegram surface over the shared flow.

    All dependencies are injected (DI; no globals). The api-coupled callables
    (``run_turn`` drives ``ConversationLoop.turn``; ``list_persona_names`` reads the
    owner's personas) are owner-scoped by the composition root, keeping this module
    api-free. The constructor + :meth:`handle` signatures are unchanged from C2 (the
    composition root + the flow tests drive them); internally the shared sequence is
    now :class:`~persona_connectors.domain.flow.SharedInboundFlow`.
    """

    def __init__(
        self,
        *,
        resolver: InboundIdentityResolver,
        linking: TelegramLinkingService,
        conversation_store: ConversationStateStore,
        connector: TelegramConnector,
        client: TelegramClient,
        list_persona_names: Callable[[str], Mapping[str, Sequence[str]]],
        run_turn: Callable[[TurnRequest], Awaitable[str]],
        now: Callable[[], datetime],
    ) -> None:
        self._linking = linking
        self._client = client
        self._now = now
        self._transport = _TelegramFlowTransport(client=client, connector=connector)
        self._shared = SharedInboundFlow(
            resolver=resolver,
            conversation_store=conversation_store,
            list_persona_names=list_persona_names,
            run_turn=run_turn,
            commands=_TELEGRAM_COMMANDS,
        )

    async def handle(self, update: dict[str, object]) -> None:
        """Handle one raw Telegram ``Update`` (the transport's ``on_update`` callback)."""
        update_id = update.get("update_id")
        _log.info(
            "telegram update received (update_id={update_id} chat={chat})",
            update_id=update_id,
            chat=_peek_conversation_key(update),
        )
        outcome = classify_update(update, now=self._now())
        if isinstance(outcome, InboundIgnore):
            # The most likely silent-drop site (R9-065) — always log why.
            _log.info(
                "telegram update {update_id} ignored: reason={reason}",
                update_id=update_id,
                reason=outcome.reason,
            )
            return
        if isinstance(outcome, InboundNonText):
            # Non-text → a friendly text-only decline (D-C2-6); no runtime turn.
            _log.info(
                "telegram update {update_id} declined: kind={kind} chat={chat}",
                update_id=update_id,
                kind=outcome.kind.value,
                chat=outcome.conversation_key,
            )
            await self._client.send_message(
                chat_id=outcome.conversation_key, text=decline_message(outcome.kind)
            )
            _log.info(
                "telegram update {update_id}: system reply sent (decline) chat={chat}",
                update_id=update_id,
                chat=outcome.conversation_key,
            )
            return
        await self._handle_text(outcome.inbound, update_id=update_id)

    async def _handle_text(self, inbound: NormalisedInbound, *, update_id: object = None) -> None:
        # AUTH CARRIER (surface-side, the binding WRITE): /start <token> redeems +
        # binds (or fails closed). The shared flow only ever READS the binding, so
        # this Telegram-specific deep-link redeem stays here, before delegation. A
        # bare /start (no token) falls through (not_a_link_attempt) and the shared
        # flow's greeting vocabulary lists the personas. NEVER log the token itself
        # (a bearer credential) — only the resulting RedeemStatus.
        redeem = self._linking.redeem_start_command(
            text=inbound.text, platform_identity=inbound.sender_id, now=self._now()
        )
        _log.info(
            "telegram update {update_id}: start-redeem status={status} chat={chat}",
            update_id=update_id,
            status=redeem.status.value,
            chat=inbound.conversation_key,
        )
        if redeem.status in (RedeemStatus.linked, RedeemStatus.failed):
            await self._client.send_message(
                chat_id=inbound.conversation_key, text=redeem.message or ""
            )
            _log.info(
                "telegram update {update_id}: system reply sent (redeem={status}) chat={chat}",
                update_id=update_id,
                status=redeem.status.value,
                chat=inbound.conversation_key,
            )
            return

        # The platform-agnostic sequence (resolve → /new → route → drive → send) is C1's.
        await self._shared.handle_text(inbound, transport=self._transport)
        _log.info(
            "telegram update {update_id}: delegated to shared flow (persona reply path) "
            "chat={chat}",
            update_id=update_id,
            chat=inbound.conversation_key,
        )
