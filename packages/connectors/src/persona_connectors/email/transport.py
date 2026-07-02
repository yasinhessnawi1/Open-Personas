"""Email's per-request FlowTransport (Spec C5, Group E) — the shared flow's I/O, no auth.

C1's :class:`~persona_connectors.domain.flow.SharedInboundFlow` drives resolve → route →
turn → send and calls a :class:`~persona_connectors.domain.flow.FlowTransport` for its three
I/O needs. Email is different from the flat-address chat platforms: the flow's
``conversation_key`` is the *thread* (the boundary), but a reply must go to the sender's
*address* with a ``Re:`` subject + ``In-Reply-To``/``References`` threading — none of which
ride on the flow's outbound. So the transport is built **per inbound**, capturing that
reply context from the parsed email, and ignores the passed ``conversation_key`` for
addressing:

- ``send_system`` — a plain bot-voice email (link-instruction / no-personas / ``/new``).
- ``send_persona`` — the rendered persona reply via ``EmailConnector.send_reply``.
- ``typing`` — a **no-op** (email has no live presence; the async posture, criterion 5).

api-free; the connector is injected.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from persona_connectors.email.render import reply_subject

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona_connectors.domain.normalise import NormalisedOutbound
    from persona_connectors.email.connector import EmailConnector

__all__ = ["EmailFlowTransport"]


@contextlib.asynccontextmanager
async def _no_typing(_conversation_key: str) -> AsyncIterator[None]:
    """A no-op typing window — email has no presence (the async posture, criterion 5)."""
    yield


class EmailFlowTransport:
    """Email's :class:`FlowTransport`, built per inbound with the reply context captured.

    ``to`` is the inbound sender's address; ``subject`` the inbound subject (→ ``Re:``);
    ``in_reply_to`` the inbound's ``Message-ID`` and ``references`` the thread chain (→ the
    reply's ``In-Reply-To``/``References``). All I/O flows through the injected connector.
    """

    def __init__(
        self,
        *,
        connector: EmailConnector,
        to: str,
        subject: str,
        in_reply_to: str | None,
        references: str | None,
    ) -> None:
        self._connector = connector
        self._to = to
        self._subject = subject
        self._in_reply_to = in_reply_to
        self._references = references

    async def send_system(self, *, conversation_key: str, text: str) -> None:  # noqa: ARG002 — the address is captured per-request, not the thread key
        await self._connector.send_system_email(
            to=self._to,
            subject=reply_subject(self._subject),
            text=text,
            in_reply_to=self._in_reply_to,
            references=self._references,
        )

    async def send_persona(self, outbound: NormalisedOutbound) -> None:
        await self._connector.send_reply(
            to=self._to,
            original_subject=self._subject,
            in_reply_to=self._in_reply_to,
            references=self._references,
            persona=outbound.persona,
            text=outbound.text,
        )

    def typing(self, conversation_key: str) -> contextlib.AbstractAsyncContextManager[None]:
        return _no_typing(conversation_key)
