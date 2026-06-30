"""The WhatsApp connector — C1 ``Connector`` (Spec C4 T1 caps, T8 send, T9 deliver).

One object that satisfies C1's ``Connector`` seam (``platform`` / ``capabilities`` /
:meth:`send` / :meth:`start` / :meth:`close`). :meth:`send` (T8) lowers a
:class:`~persona_connectors.domain.normalise.NormalisedOutbound` to WhatsApp message(s)
via :func:`~persona_connectors.whatsapp.render.render_outbound` (`*bold*` name tag +
1600-char split) and delivers them over the shared Twilio client, reporting a
:class:`~persona.delivery.DeliveryResult` — **never a silent drop** (rate-limit →
``pending``, rejection / create-time ``error_code`` → ``failed``,
D-C1-X-platform-rejection).

**The 24h-window reactive core is T9**, not here: the out-of-window 63016/131047
mapping + the async status-callback ingestion + the C0 ``deliver`` GAP-A bridge. T8 is
the in-window happy path; a create-time ``error_code`` already maps to ``failed`` here
(never silent), and T9 specialises the window codes into the template re-engagement
path. ``can_initiate_freely=False`` flags the channel for the framework.

**api-free** (the thin-adapter ideal, C1-D-1): it depends only on C1's owned-surface
contracts + the shared Twilio client. The ``whatsapp:`` address prefix is the only
WhatsApp-specific lowering; the from-address is the configured ``twilio_whatsapp_from``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from persona.delivery import DeliveryOutcome, DeliveryResult

from persona_connectors._twilio.client import TWILIO_MAX_MESSAGE_CHARS
from persona_connectors._twilio.status import WINDOW_CLOSED_DETAIL, map_delivery
from persona_connectors.domain.normalise import Capabilities, NormalisedOutbound
from persona_connectors.errors import TwilioApiError, TwilioRateLimitError
from persona_connectors.whatsapp.inbound import PLATFORM, WHATSAPP_PREFIX
from persona_connectors.whatsapp.render import render_outbound

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from persona.schema.origination import OriginatedMessage

    from persona_connectors._twilio.client import TwilioClient
    from persona_connectors.domain.conversation_model import ConversationStateStore

__all__ = ["WHATSAPP_CAPABILITIES", "WhatsAppConnector"]


def _never(_message: OriginatedMessage) -> bool:
    """The conservative v1 default: no originated message is time-sensitive (persist-only).

    WhatsApp's re-engagement template is the one path that costs money + risks a
    Marketing-category send, so the bias is toward persist-only (present-on-next-open).
    v1 ``OriginatedMessage`` carries no urgency field — a real time-sensitive signal is a
    direction-4 / A-track input, injected here when it exists. Until then: never auto-fire.
    """
    return False


# WhatsApp's channel capabilities (Spec C4, the decisions table). Rich formatting +
# a (platform-level) typing affordance + realtime push; speaks as itself (no dedicated
# author slot → the bold-prefix render tier); ``can_initiate_freely=False`` (the 24h
# customer-care window — the reactive out-of-window mapping lives in T9). Twilio enforces
# a 1600-char hard cap on WhatsApp bodies (the splitter budget). ``encoding_sensitive=
# False`` (WhatsApp counts characters, not GSM-7 segments — unlike SMS).
WHATSAPP_CAPABILITIES = Capabilities(
    supports_rich_formatting=True,
    supports_author_affordance=False,
    supports_threads=False,
    supports_typing_indicator=True,
    is_realtime_push=True,
    can_initiate_freely=False,
    max_body_chars=TWILIO_MAX_MESSAGE_CHARS,
    encoding_sensitive=False,
    requires_delivery_auth=False,
)


class WhatsAppConnector:
    """The WhatsApp adapter — implements C1's ``Connector`` (T8 send; T9 adds deliver).

    Holds no global state. Dependencies (the shared Twilio client, the configured
    from-address, the optional status-callback URL, and — for the C0 ``deliver``
    GAP-A reverse lookup — the conversation-state store + the owner-scope factory) are
    injected by the composition root, keeping this api-free.
    """

    def __init__(
        self,
        *,
        client: TwilioClient,
        from_address: str,
        conversation_store: ConversationStateStore,
        owner_scope: Callable[[str], AbstractContextManager[None]],
        status_callback_url: str = "",
        reengagement_template_sid: str = "",
        is_time_sensitive: Callable[[OriginatedMessage], bool] | None = None,
        capabilities: Capabilities = WHATSAPP_CAPABILITIES,
    ) -> None:
        self.platform = PLATFORM
        self.capabilities = capabilities
        self._client = client
        self._from = from_address
        self._template_sid = reengagement_template_sid
        self._is_time_sensitive = is_time_sensitive or _never
        self._store = conversation_store
        self._owner_scope = owner_scope
        self._status_callback = status_callback_url or None

    @staticmethod
    def _to_address(conversation_key: str) -> str:
        """Re-apply Twilio's ``whatsapp:`` prefix to a bare-E.164 conversation key."""
        if conversation_key.startswith(WHATSAPP_PREFIX):
            return conversation_key
        return f"{WHATSAPP_PREFIX}{conversation_key}"

    async def send(self, outbound: NormalisedOutbound) -> DeliveryResult:
        """Deliver a reply / in-window originated message to WhatsApp, reporting the outcome.

        Renders the `*bold*` name tag + splits to ≤1600 chars (the name header on the
        first part only), then sends the part(s) over Twilio. Maps a rate-limit to
        ``pending`` (retryable) and any rejection — an exception OR a create-time
        ``error_code`` — to ``failed`` (**never a silent drop**; D-C1-X-platform-rejection).
        A create-time ``error_code`` is routed through the shared :func:`map_delivery`, so
        an out-of-window code (63016/131047) arriving **synchronously** carries the same
        window-distinct detail as the async status-callback path (T9 single ingestion).
        """
        to = self._to_address(outbound.conversation_key)
        parts = render_outbound(outbound.persona, outbound.text)
        try:
            for part in parts:
                result = await self._client.send_message(
                    to=to, from_=self._from, body=part, status_callback=self._status_callback
                )
                if result.error_code is not None:
                    return map_delivery(
                        channel=PLATFORM, status=result.status, error_code=result.error_code
                    )
        except TwilioRateLimitError:
            return DeliveryResult(
                outcome=DeliveryOutcome.PENDING,
                channel=PLATFORM,
                detail="twilio rate-limited; retry later",
            )
        except TwilioApiError:
            return DeliveryResult(
                outcome=DeliveryOutcome.FAILED, channel=PLATFORM, detail="whatsapp send rejected"
            )
        return DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel=PLATFORM)

    async def deliver(self, message: OriginatedMessage) -> DeliveryResult:
        """Deliver a C0-originated message to WhatsApp (the GAP-A bridge).

        Resolves the originated message's internal ``conversation_id`` to its WhatsApp
        channel (owner-scoped, via the framework's ``resolve_channel``), assembles a
        ``NormalisedOutbound``, and sends it. No conversation / no resolvable WhatsApp
        channel → ``pending`` (the content is already durably persisted upstream — C0
        D-C0-4 — so it is **present on next open, never lost**). An out-of-window
        free-form origination then maps via :meth:`send` to ``failed`` + the window
        detail (T10's template signal), the message still persisted.
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
        result = await self.send(outbound)
        if result.detail == WINDOW_CLOSED_DETAIL:
            return await self._maybe_reengage(message, ref.channel_key, base=result)
        return result

    async def _maybe_reengage(
        self, message: OriginatedMessage, channel_key: str, *, base: DeliveryResult
    ) -> DeliveryResult:
        """Conservatively nudge an out-of-window C0 with the approved template (D-C4-2).

        Fires the re-engagement template ONLY when (a) a template is configured AND (b)
        this originated message is judged **time-sensitive** (default: never — persist-only,
        the billable Marketing-category send stays opt-in). Otherwise returns ``base``
        unchanged (``failed`` + the window detail; the content is durably persisted,
        present-on-next-open). The free-form content was NOT delivered either way — the
        template is a re-engagement nudge, reflected in the detail (observability), never a
        claim the content reached the user.
        """
        if not self._template_sid or not self._is_time_sensitive(message):
            return base  # non-urgent / no template → persist-only (present-on-next-open)
        nudge = await self.send_reengagement_template(
            channel_key, persona_name=message.persona.display_name
        )
        sent = nudge.outcome is DeliveryOutcome.DELIVERED
        detail = (
            "send-window closed; re-engagement template sent"
            if sent
            else "send-window closed; re-engagement template send failed"
        )
        return DeliveryResult(outcome=DeliveryOutcome.FAILED, channel=PLATFORM, detail=detail)

    async def send_reengagement_template(
        self, conversation_key: str, *, persona_name: str | None = None
    ) -> DeliveryResult:
        """Send the approved WhatsApp re-engagement template via ``ContentSid`` (D-C4-2).

        The net-new out-of-window send path: a pre-approved template (raw-body templates
        were disallowed by Twilio on 2025-04-01, so this is ``ContentSid`` +
        ``ContentVariables``). Reports the **template's** delivery outcome (so the caller
        sees whether the nudge went out) — ``delivered`` on success, ``pending`` on
        rate-limit, ``failed`` on rejection or no configured template. ``persona_name``, if
        given, binds the template's first variable (``{{1}}`` — "an update from <name>").
        """
        if not self._template_sid:
            return DeliveryResult(
                outcome=DeliveryOutcome.FAILED,
                channel=PLATFORM,
                detail="no re-engagement template configured",
            )
        variables = json.dumps({"1": persona_name}) if persona_name else None
        try:
            result = await self._client.send_message(
                to=self._to_address(conversation_key),
                from_=self._from,
                content_sid=self._template_sid,
                content_variables=variables,
                status_callback=self._status_callback,
            )
        except TwilioRateLimitError:
            return DeliveryResult(
                outcome=DeliveryOutcome.PENDING, channel=PLATFORM, detail="twilio rate-limited"
            )
        except TwilioApiError:
            return DeliveryResult(
                outcome=DeliveryOutcome.FAILED, channel=PLATFORM, detail="template send rejected"
            )
        if result.error_code is not None:
            return map_delivery(
                channel=PLATFORM, status=result.status, error_code=result.error_code
            )
        return DeliveryResult(outcome=DeliveryOutcome.DELIVERED, channel=PLATFORM)

    async def start(self) -> None:
        """No-op in v1 — each send is a stateless Twilio call; receive is the flow's concern."""

    async def close(self) -> None:
        """No-op in v1 — the injected HTTP client's lifecycle is owned by the composition root."""
