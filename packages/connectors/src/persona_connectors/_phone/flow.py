"""The shared phone inbound-flow orchestrator (Spec C4 T13) — WhatsApp + SMS surface I/O.

The phone-channel analogue of ``telegram/flow.py``'s :class:`InboundFlow`: it wires one
inbound Twilio message (WhatsApp **or** SMS — near-identical, so ONE class parameterized
by the platform's surface) through the framework: classify → (ignore | non-text decline |
OTP redeem | delegate to the shared flow). The platform-agnostic sequence (resolve →
``/new`` → route → drive the turn → send) is C1's
:class:`~persona_connectors.domain.flow.SharedInboundFlow`, shared with Telegram /
Discord / Slack; this module supplies only the **phone surface**: the inbound
classification (injected ``classify_inbound``), the non-text decline (injected
``decline_message`` over the platform's ``send_message``), the **auth carrier** (the
texted-back OTP redeem — the one binding *write*, surface-side; the shared flow only ever
*reads* the binding), and the platform I/O behind the injected
:class:`~persona_connectors.domain.flow.FlowTransport`.

**The OTP carrier mirrors Telegram's ``/start`` redeem-before-shared-flow EXACTLY** — the
ONLY difference is the carrier shape: a plain-text code (the user texts it back), not a
``/start <token>`` deep link. The bound ``platform_identity`` is the **webhook-derived**
``sender_id`` from the :class:`~persona_connectors.domain.normalise.NormalisedInbound`
(the signature-verified Twilio ``From``), NEVER a number parsed from the editable text —
the C1-D-5 spoofing guard. On ``linked`` → send the confirmation + return; on ``failed``
→ send the retry copy + return; on ``not_a_link_attempt`` → delegate to the shared flow
(which resolves identity → link-instruction if unlinked, else routes).

WhatsApp and SMS are parameterized differently (the platform's ``classify_inbound`` and
non-text taxonomy, its :class:`FlowTransport`, its ``decline_message``, its platform key)
but share this orchestration verbatim — the rule-of-three thin-adapter promise (criterion
10). **api-free**: every api-coupled callable is injected by the composition root.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Generic, Protocol, TypeVar

from persona_connectors._phone.linking import RedeemStatus
from persona_connectors.errors import IdentityNotLinkedError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from persona_connectors._phone.linking import PhoneLinkingService, RedeemResult
    from persona_connectors.domain.flow import FlowTransport, SharedInboundFlow
    from persona_connectors.domain.normalise import NormalisedInbound

__all__ = ["PhoneInboundFlow"]

# The platform's non-text taxonomy (WhatsApp's {voice, media, unknown} vs SMS's
# {media, unknown}) — both ``StrEnum`` subclasses. Binding the flow's ``kind`` + its
# ``decline_message`` to ONE type var keeps each adapter's decline copy type-safe.
_KindT = TypeVar("_KindT", bound=StrEnum)
# The covariant view of the same kind for the read-only non-text protocol (a property
# is an output position, so the protocol's type var must be covariant).
_KindCoT = TypeVar("_KindCoT", bound=StrEnum, covariant=True)


class _InboundTextLike(Protocol):
    """The classifier's text outcome — carries the C1 ``NormalisedInbound``."""

    @property
    def inbound(self) -> NormalisedInbound: ...


class _InboundNonTextLike(Protocol[_KindCoT]):
    """The classifier's non-text outcome — what a decline reply needs."""

    @property
    def kind(self) -> _KindCoT: ...

    @property
    def conversation_key(self) -> str: ...


class PhoneInboundFlow(Generic[_KindT]):
    """Orchestrates one inbound phone (WhatsApp / SMS) message over the shared flow.

    All dependencies are injected (DI; no globals). The platform surface is supplied as
    data: ``classify_inbound`` (the platform's classifier), ``transport`` (its
    :class:`FlowTransport`), ``decline_message`` (its non-text copy), and the
    platform-typed ``InboundText`` / ``InboundNonText`` classes (for the ``isinstance``
    branch). The auth carrier
    (:class:`~persona_connectors._phone.linking.PhoneLinkingService`) and the api-coupled
    callables behind ``shared`` are owner-scoped by the composition root, keeping this
    module api-free.

    The non-text decline is sent through the **transport's** ``send_system`` (not a raw
    client call) — the bot speaking, no persona — so the platform's address re-prefixing
    (``whatsapp:`` for WhatsApp; none for SMS) is owned in ONE place and correct for both
    channels.
    """

    def __init__(
        self,
        *,
        platform: str,
        classify_inbound: Callable[..., object],
        inbound_text_type: type[object],
        inbound_non_text_type: type[object],
        decline_message: Callable[[_KindT], str],
        linking: PhoneLinkingService,
        shared: SharedInboundFlow,
        transport: FlowTransport,
        now: Callable[[], datetime],
    ) -> None:
        self._platform = platform
        self._classify = classify_inbound
        self._inbound_text_type = inbound_text_type
        self._inbound_non_text_type = inbound_non_text_type
        self._decline_message = decline_message
        self._linking = linking
        self._shared = shared
        self._transport = transport
        self._now = now

    async def handle(self, params: Mapping[str, str]) -> None:
        """Handle one verified Twilio inbound form POST (the app's ``on_inbound`` callback).

        The params come from the signature-verified webhook (the app rejects-before-act),
        so the classified ``sender_id`` is a trusted identity. Branches: ignore → noop;
        non-text → the platform decline; text → :meth:`_handle_text`.
        """
        outcome = self._classify(params, now=self._now())
        if isinstance(outcome, self._inbound_non_text_type):
            non_text: _InboundNonTextLike[_KindT] = outcome  # type: ignore[assignment]
            await self._transport.send_system(
                conversation_key=non_text.conversation_key,
                text=self._decline_message(non_text.kind),
            )
            return
        if isinstance(outcome, self._inbound_text_type):
            text_outcome: _InboundTextLike = outcome  # type: ignore[assignment]
            await self._handle_text(text_outcome.inbound)
        # InboundIgnore (or anything else) → silently skipped, no reply.

    async def _handle_text(self, inbound: NormalisedInbound) -> None:
        # AUTH CARRIER (surface-side, the binding WRITE): a texted-back OTP code redeems +
        # binds (or fails closed). ``platform_identity`` is the webhook-verified sender_id
        # (NEVER the editable text — the C1-D-5 spoofing guard).
        #
        # The redeem is a LINKING action — meaningful ONLY for an UNLINKED sender. A LINKED
        # sender's message is normal conversation, even when it happens to be code-shaped
        # ("WHATEVER", "deadbeef" are 8 Crockford-base32 chars): route it, never a confusing
        # "code didn't work". Re-linking an active number requires an explicit unlink first
        # (C1-D-5 partial-active UNIQUE), so a linked sender has no outstanding code to
        # redeem — gating the redeem unlinked-only removes the false-positive with no real
        # flow lost. An unlinked non-code message falls through (not_a_link_attempt) to the
        # shared flow, which sends the link-instruction (zero access) for an unlinked sender.
        if not self._is_linked(inbound.sender_id):
            redeem: RedeemResult = self._linking.redeem_texted_code(
                text=inbound.text, platform_identity=inbound.sender_id, now=self._now()
            )
            if redeem.status in (RedeemStatus.linked, RedeemStatus.failed):
                await self._transport.send_system(
                    conversation_key=inbound.conversation_key, text=redeem.message or ""
                )
                return

        # The platform-agnostic sequence (resolve → /new → route → drive → send) is C1's.
        await self._shared.handle_text(inbound, transport=self._transport)

    def _is_linked(self, sender_id: str) -> bool:
        """Whether the webhook-verified sender already has a live binding.

        A pre-auth identity read (the same lookup the shared flow's resolver does) — when it
        resolves, the OTP redeem is skipped (the sender is linked; their message is
        conversation, not a code). Unlinked → ``False``, so the redeem (link path) runs.
        """
        try:
            self._linking.resolve_owner(platform_identity=sender_id)
        except IdentityNotLinkedError:
            return False
        return True
