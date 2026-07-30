"""The email inbound-flow orchestrator (Spec C5, Group E) — the reachable chain's core.

The email analogue of ``_phone/flow.py``: wires one parsed inbound email through the
framework. The order is the load-bearing part of the spec (D-C5-5): the webhook app runs
**B1** (Basic-Auth) then parses; this orchestrator then runs **B2** (the sender-authenticity
verdict) — so ``handle`` is only ever called on a B1-authenticated payload, and B2 trusts
these headers only because of that (B1's Basic-Auth is what makes them genuinely Postmark's;
see :mod:`persona_connectors._postmark.authentication` for how B2 itself decides). Then:
attachment-only acknowledge → (unlinked) OTP redeem → C1's
:class:`~persona_connectors.domain.flow.SharedInboundFlow` (resolve → route → turn → send),
with the plus-address ``envelope_persona_tag`` supplied (A2 / D-C5-3).

**B2, updated for reality (R9-072):** Postmark's inbound-parse payload never carries an
``Authentication-Results`` header, so the original ``dmarc == "pass"`` gate could never pass
on real traffic — inbound email was dead by construction. B2 now also accepts
SpamAssassin's ``DKIM_VALID_AU`` token (``X-Spam-Tests``): a valid DKIM signature aligned
with the ``From:`` domain, which is exactly DMARC's DKIM leg computed outside an
``Authentication-Results`` header. ``Authentication-Results`` is still honoured first and is
authoritative when present (future-proof + the legacy path). **SPF alone remains
categorically insufficient** in both paths — it authenticates the envelope sender, not
``From:``, and accepting it would let ``From: victim@`` through on the attacker's own
envelope domain. See the authentication module's docstring for the full reasoning and the
explicit ``dmarc=fail`` vs ``DKIM_VALID_AU`` ruling.

**Anti-spoofing is fail-closed:** a sender whose ``From`` is not authenticated by the above
gets **zero access** — no bind, no turn, and no reply (a reply would go to the spoofed
address). The OTP redeem binds the **B1+B2-validated** ``sender_id``, never the editable
body (C1-D-5).

api-free: every api-coupled callable behind ``shared`` is owner-scoped by the composition root.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from persona.logging import get_logger

from persona_connectors._phone.linking import RedeemStatus
from persona_connectors._postmark.authentication import (
    parse_received_spf_verdict,
    parse_spam_test_tokens,
    sender_is_authentic_from_headers,
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
        # sender we can't prove is genuinely `From:` this address gets ZERO access: no bind,
        # no turn, no reply. See _postmark.authentication.sender_is_authentic_from_headers
        # for the decision (Authentication-Results dmarc=pass if ever present, else Postmark's
        # real aligned signal: X-Spam-Tests' DKIM_VALID_AU).
        headers = {
            "authentication-results": parsed.authentication_results,
            "x-spam-tests": parsed.spam_tests,
        }
        if not sender_is_authentic_from_headers(headers):
            # Log the OBSERVED signals, not just the refusal — tokens only, never message
            # content. Distinguishes "sender genuinely failed" from "the signal we need was
            # never present" (the historical failure mode: relying on Authentication-Results
            # alone made this gate permanently closed on real Postmark traffic).
            _spam_tokens = parse_spam_test_tokens(parsed.spam_tests)
            _log.warning(
                "email inbound rejected: From not authenticated "
                "(fp={fp} auth_results_present={ar_present} "
                "dkim_valid_au={au} spam_tests={tests} received_spf={spf})",
                fp=_fingerprint(parsed.inbound.sender_id),
                ar_present=parsed.authentication_results is not None,
                au="DKIM_VALID_AU" in _spam_tokens,
                tests=", ".join(sorted(_spam_tokens)) or "<absent>",
                spf=parse_received_spf_verdict(parsed.received_spf) or "<absent>",
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
        # R9-077 (observability first): every branch below used to return SILENTLY, so a
        # message that reached this method and never produced a turn left no trace at all —
        # production showed `POST /email/webhook 200` and then 23 minutes of nothing. Each
        # branch now names itself, with the sender fingerprinted (never the address, never
        # the message content, never the code).
        fp = _fingerprint(inbound.sender_id)
        if not self._is_linked(inbound.sender_id):
            redeem = self._linking.redeem_emailed_code(
                text=inbound.text, platform_identity=inbound.sender_id, now=self._now()
            )
            if redeem.status in (RedeemStatus.linked, RedeemStatus.failed):
                _log.info(
                    "email inbound handled by the OTP carrier: no turn this message "
                    "(fp={fp} redeem={status})",
                    fp=fp,
                    status=redeem.status.value,
                )
                await transport.send_system(
                    conversation_key=inbound.conversation_key, text=redeem.message or ""
                )
                return
            _log.info(
                "email inbound from an UNLINKED sender, not a code — the shared flow will "
                "reply with the link instruction (fp={fp} redeem={status})",
                fp=fp,
                status=redeem.status.value,
            )
        elif not inbound.text.strip():
            # A LINKED sender with an empty body: acknowledge an attachment-only email
            # gracefully (criterion 9), else nothing to do. (An UNLINKED empty body falls
            # through above to the shared flow's link-instruction — zero access, not an ack.)
            _log.warning(
                "email inbound has no usable text after quote-stripping: no turn "
                "(fp={fp} attachments={attachments})",
                fp=fp,
                attachments=parsed.has_attachments,
            )
            if parsed.has_attachments:
                await transport.send_system(
                    conversation_key=inbound.conversation_key, text=_ATTACHMENT_ACK
                )
            return

        # The platform-agnostic sequence (resolve → /new → route → drive → send) is C1's; the
        # plus-address tag (A2) selects the persona deterministically when present.
        _log.info(
            "email inbound accepted → shared flow (fp={fp} persona_tag={tag})",
            fp=fp,
            tag=parsed.envelope_persona_tag or "<none>",
        )
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
