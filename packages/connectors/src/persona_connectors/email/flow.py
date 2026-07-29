"""The email inbound-flow orchestrator (Spec C5, Group E) — the reachable chain's core.

The email analogue of ``_phone/flow.py``: wires one parsed inbound email through the
framework. The order is the load-bearing part of the spec (D-C5-5): the webhook app runs
**B1** (Basic-Auth) then parses; this orchestrator then runs **B2** (the DMARC verdict) —
so ``handle`` is only ever called on a B1-authenticated payload, and it trusts the verdict
only because of that. Then: attachment-only acknowledge → (unlinked) OTP redeem → C1's
:class:`~persona_connectors.domain.flow.SharedInboundFlow` (resolve → route → turn → send),
with the plus-address ``envelope_persona_tag`` supplied (A2 / D-C5-3).

**Anti-spoofing is fail-closed:** a sender whose ``From`` is not DMARC-authentic gets **zero
access** — no bind, no turn, and no reply (a reply would go to the spoofed address). The OTP
redeem binds the **B1+B2-validated** ``sender_id``, never the editable body (C1-D-5).

api-free: every api-coupled callable behind ``shared`` is owner-scoped by the composition root.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from persona.logging import get_logger

from persona_connectors._phone.linking import RedeemStatus
from persona_connectors._postmark.authentication import (
    parse_authentication_results,
    sender_is_authentic,
)
from persona_connectors.email.transport import EmailFlowTransport
from persona_connectors.errors import IdentityNotLinkedError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from persona_connectors.domain.flow import SharedInboundFlow
    from persona_connectors.email.connector import EmailConnector
    from persona_connectors.email.inbound import ParsedEmail
    from persona_connectors.email.linking import EmailLinkingService

__all__ = ["EmailInboundFlow"]

_log = get_logger("connectors.email_flow")

_ATTACHMENT_ACK = (
    "Thanks for your message. I work over the text of an email, so I've read what you "
    "wrote — but I can't open attachments yet."
)


def _fingerprint(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


class EmailInboundFlow:
    """Orchestrates one parsed inbound email over the shared flow (B2 → redeem → shared).

    Dependencies injected (DI; no globals): the connector (for the per-request transport),
    the email OTP carrier, the C1 ``SharedInboundFlow`` (owner-scoped api-coupled callables),
    and ``now``. Built once; ``handle`` runs per inbound.
    """

    def __init__(
        self,
        *,
        connector: EmailConnector,
        linking: EmailLinkingService,
        shared: SharedInboundFlow,
        now: Callable[[], datetime],
    ) -> None:
        self._connector = connector
        self._linking = linking
        self._shared = shared
        self._now = now

    async def handle(self, parsed: ParsedEmail) -> None:
        """Handle one B1-authenticated, parsed inbound email (the app's ``on_inbound``)."""
        # B2 — sender authenticity (the payload is already B1-authenticated by the app). A
        # spoofed / non-DMARC ``From`` gets ZERO access: no bind, no turn, no reply.
        if not sender_is_authentic(parsed.authentication_results):
            # Log the OBSERVED verdicts, not just the refusal. "not DMARC-authentic" alone
            # cannot distinguish the two very different causes: the sender genuinely failed
            # DMARC (working as intended) vs the ESP never stamped a `dmarc=` token at all
            # (in which case this gate can NEVER pass and inbound email is dead by
            # construction). These are verdict tokens (`pass`/`fail`/`none`/absent), NOT
            # message content — safe to log, and the only way to tell those apart from prod.
            _verdicts = parse_authentication_results(parsed.authentication_results)
            _log.warning(
                "email inbound rejected: From not DMARC-authentic "
                "(fp={fp} dmarc={dmarc} spf={spf} dkim={dkim} header_present={present})",
                fp=_fingerprint(parsed.inbound.sender_id),
                dmarc=_verdicts.dmarc or "<absent>",
                spf=_verdicts.spf or "<absent>",
                dkim=_verdicts.dkim or "<absent>",
                present=parsed.authentication_results is not None,
            )
            return

        inbound = parsed.inbound
        transport = EmailFlowTransport(
            connector=self._connector,
            to=inbound.sender_id,
            subject=parsed.subject,
            in_reply_to=inbound.message_id,
            references=self._reply_references(parsed),
        )

        # AUTH CARRIER (unlinked-only, the binding WRITE): an emailed-back OTP code redeems +
        # binds the B1+B2-validated From (never the editable body — C1-D-5). A linked sender's
        # code-shaped message is normal conversation, so the redeem is gated unlinked-only. An
        # unlinked non-code message falls through to the shared flow (its link-instruction).
        if not self._is_linked(inbound.sender_id):
            redeem = self._linking.redeem_emailed_code(
                text=inbound.text, platform_identity=inbound.sender_id, now=self._now()
            )
            if redeem.status in (RedeemStatus.linked, RedeemStatus.failed):
                await transport.send_system(
                    conversation_key=inbound.conversation_key, text=redeem.message or ""
                )
                return
        elif not inbound.text.strip():
            # A LINKED sender with an empty body: acknowledge an attachment-only email
            # gracefully (criterion 9), else nothing to do. (An UNLINKED empty body falls
            # through above to the shared flow's link-instruction — zero access, not an ack.)
            if parsed.has_attachments:
                await transport.send_system(
                    conversation_key=inbound.conversation_key, text=_ATTACHMENT_ACK
                )
            return

        # The platform-agnostic sequence (resolve → /new → route → drive → send) is C1's; the
        # plus-address tag (A2) selects the persona deterministically when present.
        await self._shared.handle_text(
            inbound, transport=transport, envelope_persona_tag=parsed.envelope_persona_tag
        )

    def _is_linked(self, sender_id: str) -> bool:
        """Whether the validated sender already has a live binding (skip the redeem if so)."""
        try:
            self._linking.resolve_owner(platform_identity=sender_id)
        except IdentityNotLinkedError:
            return False
        return True

    @staticmethod
    def _reply_references(parsed: ParsedEmail) -> str | None:
        """The reply's ``References`` — the inbound chain plus the inbound's own ``Message-ID``."""
        references = parsed.references
        message_id = parsed.inbound.message_id
        if references and message_id:
            return f"{references} {message_id}"
        return references or message_id or None
