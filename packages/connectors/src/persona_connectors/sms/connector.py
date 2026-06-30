"""The SMS connector — C1 ``Connector`` + C0 ``MessageDeliverer`` (Spec C4 T1 caps, T12).

One object that satisfies both seams. :meth:`send` lowers a ``NormalisedOutbound`` to a
single SMS body (the plain-prefix ``Name: body`` tag + the GSM-7/UCS-2-aware segment cap
via :func:`~persona_connectors.sms.render.render_outbound`) and sends it as ONE Twilio
message (Twilio auto-concatenates the segments + bills per segment — the cost is recorded
from the status callback by :mod:`~persona_connectors.sms.cost`, the T9 single seam).
:meth:`deliver` is the C0 GAP-A bridge.

**SMS has no 24h window** (``can_initiate_freely=True``): a C0 originated SMS sends
**regardless** of when the user last messaged (criterion 5) — there is deliberately NO
window gate or template path here (that is WhatsApp's asymmetry, not SMS's). Outcomes are
never-silent (delivered / pending / failed), mapped through the shared :func:`map_delivery`
— but the WhatsApp window codes can never arise for SMS, so a generic SMS failure stays
plain ``failed``.

**api-free** (the thin-adapter ideal, C1-D-1). Addresses are bare E.164 (no prefix); the
from-address is the configured ``twilio_sms_from``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.delivery import DeliveryOutcome, DeliveryResult

from persona_connectors._twilio.client import TWILIO_MAX_MESSAGE_CHARS
from persona_connectors._twilio.status import map_delivery
from persona_connectors.domain.normalise import Capabilities, NormalisedOutbound
from persona_connectors.errors import TwilioApiError, TwilioRateLimitError
from persona_connectors.sms.inbound import PLATFORM
from persona_connectors.sms.render import render_outbound

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from persona.schema.origination import OriginatedMessage

    from persona_connectors._twilio.client import TwilioClient
    from persona_connectors.domain.conversation_model import ConversationStateStore

__all__ = ["SMS_CAPABILITIES", "SmsConnector"]

# Default per-message segment cap (D-C4-3) — bounds runaway SMS cost; tunable via config.
_DEFAULT_MAX_SEGMENTS = 3

# SMS's channel capabilities (Spec C4, the decisions table). The floor: no rich
# formatting / no typing / no threads; realtime push; ``can_initiate_freely=True`` (no
# 24h window); ``encoding_sensitive=True`` (GSM-7 → UCS-2 changes the segment budget);
# ``max_body_chars`` is Twilio's hard cap. No author slot → the plain-prefix render tier.
SMS_CAPABILITIES = Capabilities(
    supports_rich_formatting=False,
    supports_author_affordance=False,
    supports_threads=False,
    supports_typing_indicator=False,
    is_realtime_push=True,
    can_initiate_freely=True,
    max_body_chars=TWILIO_MAX_MESSAGE_CHARS,
    encoding_sensitive=True,
    requires_delivery_auth=False,
)


class SmsConnector:
    """The SMS adapter — implements C1's ``Connector`` + C0's ``MessageDeliverer``.

    Holds no global state. Dependencies (the shared Twilio client, the from-address, the
    conversation-state store + owner-scope for the GAP-A ``deliver``, the optional
    status-callback URL, and the segment cap) are injected by the composition root.
    """

    def __init__(
        self,
        *,
        client: TwilioClient,
        from_address: str,
        conversation_store: ConversationStateStore,
        owner_scope: Callable[[str], AbstractContextManager[None]],
        status_callback_url: str = "",
        max_segments: int = _DEFAULT_MAX_SEGMENTS,
        capabilities: Capabilities = SMS_CAPABILITIES,
    ) -> None:
        self.platform = PLATFORM
        self.capabilities = capabilities
        self._client = client
        self._from = from_address
        self._store = conversation_store
        self._owner_scope = owner_scope
        self._status_callback = status_callback_url or None
        self._max_segments = max_segments

    async def send(self, outbound: NormalisedOutbound) -> DeliveryResult:
        """Deliver a reply / originated SMS, reporting the outcome (no window — criterion 5).

        Renders the plain-prefix ``Name: body`` + the GSM-7/UCS-2 segment cap, then sends
        ONE Twilio message to the bare-E.164 conversation key. Maps a rate-limit to
        ``pending`` and any rejection (exception OR a create-time ``error_code``) to
        ``failed`` — never silent. There is **no window gate**: an SMS sends regardless of
        when the user last messaged.
        """
        body = render_outbound(outbound.persona, outbound.text, max_segments=self._max_segments)
        try:
            result = await self._client.send_message(
                to=outbound.conversation_key,
                from_=self._from,
                body=body,
                status_callback=self._status_callback,
            )
        except TwilioRateLimitError:
            return DeliveryResult(
                outcome=DeliveryOutcome.PENDING,
                channel=PLATFORM,
                detail="twilio rate-limited; retry later",
            )
        except TwilioApiError:
            return DeliveryResult(
                outcome=DeliveryOutcome.FAILED, channel=PLATFORM, detail="sms send rejected"
            )
        if result.error_code is not None:
            return map_delivery(
                channel=PLATFORM, status=result.status, error_code=result.error_code
            )
        return DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel=PLATFORM)

    async def deliver(self, message: OriginatedMessage) -> DeliveryResult:
        """Deliver a C0-originated SMS (the GAP-A bridge) — freely, no window (criterion 5).

        Resolves the originated message's ``conversation_id`` to its SMS channel
        (owner-scoped), assembles a ``NormalisedOutbound``, and sends it. No conversation /
        no resolvable SMS channel → ``pending`` (content durably persisted upstream, C0
        D-C0-4 — never lost). Unlike WhatsApp, there is **no window check** — the message
        sends whenever it is produced (the phone-platform asymmetry, surfaced not glossed).
        """
        if message.conversation_id is None:
            return DeliveryResult(
                outcome=DeliveryOutcome.PENDING, channel=PLATFORM, detail="no conversation"
            )
        with self._owner_scope(message.owner_user_id):
            ref = self._store.resolve_channel(conversation_id=message.conversation_id)
        if ref is None or ref.platform != PLATFORM:
            return DeliveryResult(
                outcome=DeliveryOutcome.PENDING,
                channel=PLATFORM,
                detail="no connector channel for conversation",
            )
        outbound = NormalisedOutbound(
            persona=message.persona,
            text=message.content,
            conversation_key=ref.channel_key,
            reply_to_message_id=None,
        )
        return await self.send(outbound)

    async def start(self) -> None:
        """No-op in v1 — each send is a stateless Twilio call; receive is the flow's concern."""

    async def close(self) -> None:
        """No-op in v1 — the injected HTTP client's lifecycle is owned by the composition root."""
