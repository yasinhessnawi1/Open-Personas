"""The email connector — C1 ``Connector`` + C0 ``MessageDeliverer`` (Spec C5, Group C).

One object satisfying both seams over the Postmark send client. Email is the top render
tier: the persona rides in the ``From`` display-name (author-affordance, D-C5-3), the body
stays plain text (v1). Two send shapes, because email — unlike the flat-address chat
platforms — separates the *conversation boundary* (the thread) from the *delivery target*
(the address):

- :meth:`send_reply` — the inbound-reply path (used by the webhook's per-request transport,
  Group E): a real ``Re:`` subject + ``In-Reply-To``/``References`` threading (criterion 8).
- :meth:`send` — the C1 protocol / C0-originated path: ``conversation_key`` is the recipient
  address, a clear name-identified subject, a fresh thread (criteria 5/7).

**Email is the FREEST C0 channel** (``can_initiate_freely=True``): :meth:`deliver` sends an
originated email **regardless of any window** (the inverse of WhatsApp — criterion 7), to the
owner's linked address. Outcomes are never silent (delivered / pending / failed), mapped from
the Postmark result. api-free (the thin-adapter ideal, C1-D-1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.delivery import DeliveryOutcome, DeliveryResult

from persona_connectors.domain.normalise import Capabilities, NormalisedOutbound
from persona_connectors.email.render import (
    originated_subject,
    render_from,
    render_system_from,
    reply_subject,
    thread_headers,
)
from persona_connectors.errors import PostmarkApiError, PostmarkRateLimitError

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.schema.origination import OriginatedMessage, PersonaIdentityTag

    from persona_connectors._postmark.client import PostmarkClient

__all__ = ["EMAIL_CAPABILITIES", "PLATFORM", "EmailConnector"]

# The opaque platform key (DeliveryRouter registration + NormalisedInbound.platform).
PLATFORM = "email"

# Email's channel capabilities. Author-affordance (From display-name) is the top render
# tier; async (no realtime push, no typing); **can_initiate_freely=True** — the freest C0
# channel, no window/cost (criterion 7); native threading; unbounded body; sending needs
# domain auth (SPF/DKIM/DMARC on our Postmark domain — requires_delivery_auth).
EMAIL_CAPABILITIES = Capabilities(
    supports_rich_formatting=False,
    supports_author_affordance=True,
    supports_threads=True,
    supports_typing_indicator=False,
    is_realtime_push=False,
    can_initiate_freely=True,
    max_body_chars=None,
    encoding_sensitive=False,
    requires_delivery_auth=True,
)


class EmailConnector:
    """The email adapter — implements C1's ``Connector`` + C0's ``MessageDeliverer``.

    Dependencies are injected (DI; no globals): the Postmark client, our shared inbound
    ``from_address`` (the persona rides in its display-name), and ``recipient_for`` — an
    owner-id → linked-email resolver the composition root backs with the link store (the
    C0-originated recipient; the reply path already knows the address from the inbound).
    """

    def __init__(
        self,
        *,
        client: PostmarkClient,
        from_address: str,
        recipient_for: Callable[[str], str | None],
        capabilities: Capabilities = EMAIL_CAPABILITIES,
    ) -> None:
        self.platform = PLATFORM
        self.capabilities = capabilities
        self._client = client
        self._from = from_address
        self._recipient_for = recipient_for

    async def _send(
        self,
        *,
        to: str,
        subject: str,
        persona: PersonaIdentityTag,
        text: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> DeliveryResult:
        """Render + send one email, mapping the Postmark outcome (never silent)."""
        headers = thread_headers(references=references, in_reply_to=in_reply_to)
        try:
            await self._client.send_email(
                from_=render_from(persona, address=self._from),
                to=to,
                subject=subject,
                text_body=text,
                headers=headers or None,
            )
        except PostmarkRateLimitError:
            return DeliveryResult(
                outcome=DeliveryOutcome.PENDING,
                channel=PLATFORM,
                detail="postmark rate-limited; retry later",
            )
        except PostmarkApiError:
            return DeliveryResult(
                outcome=DeliveryOutcome.FAILED, channel=PLATFORM, detail="email send rejected"
            )
        return DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel=PLATFORM)

    async def send(self, outbound: NormalisedOutbound) -> DeliveryResult:
        """Send an outbound (the C1 protocol / C0-originated path) — no window (criterion 5/7).

        ``conversation_key`` is the recipient address; the subject is the clear,
        name-identified originated subject; ``reply_to_message_id`` threads it if present
        (else a fresh thread). Reports pending/failed — never silent.
        """
        return await self._send(
            to=outbound.conversation_key,
            subject=originated_subject(outbound.persona),
            persona=outbound.persona,
            text=outbound.text,
            in_reply_to=outbound.reply_to_message_id,
        )

    async def send_reply(
        self,
        *,
        to: str,
        original_subject: str | None,
        in_reply_to: str | None,
        references: str | None,
        persona: PersonaIdentityTag,
        text: str,
    ) -> DeliveryResult:
        """Send a reply email (inbound path): one ``Re:`` + thread headers (criterion 8)."""
        return await self._send(
            to=to,
            subject=reply_subject(original_subject),
            persona=persona,
            text=text,
            in_reply_to=in_reply_to,
            references=references,
        )

    async def send_system_email(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> DeliveryResult:
        """Send a plain system email — the bot speaking, no persona (link-instruction, etc.).

        The ``From`` is our product identity (no persona display-name); used by the flow
        transport's ``send_system`` for the link-instruction / no-personas / ``/new`` replies.
        """
        headers = thread_headers(references=references, in_reply_to=in_reply_to)
        try:
            await self._client.send_email(
                from_=render_system_from(self._from),
                to=to,
                subject=subject,
                text_body=text,
                headers=headers or None,
            )
        except PostmarkRateLimitError:
            return DeliveryResult(
                outcome=DeliveryOutcome.PENDING, channel=PLATFORM, detail="postmark rate-limited"
            )
        except PostmarkApiError:
            return DeliveryResult(
                outcome=DeliveryOutcome.FAILED, channel=PLATFORM, detail="system email rejected"
            )
        return DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel=PLATFORM)

    async def deliver(self, message: OriginatedMessage) -> DeliveryResult:
        """Deliver a C0-originated email — the FREEST channel: no window (criterion 7).

        The recipient is the owner's linked email (``recipient_for``); no linked email →
        ``pending`` (the content is durably persisted upstream, C0 D-C0-4 — never lost).
        Unlike WhatsApp there is **no window check** — a persona emails unprompted anytime.
        """
        to = self._recipient_for(message.owner_user_id)
        if to is None:
            return DeliveryResult(
                outcome=DeliveryOutcome.PENDING,
                channel=PLATFORM,
                detail="no linked email for owner",
            )
        return await self.send(
            NormalisedOutbound(persona=message.persona, text=message.content, conversation_key=to)
        )

    async def start(self) -> None:
        """No-op — each send is a stateless Postmark call; receive is the webhook's concern."""

    async def close(self) -> None:
        """No-op — the injected httpx client's lifecycle is owned by the composition root."""
