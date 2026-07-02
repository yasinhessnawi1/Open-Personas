"""Email-address verification linking (Spec C5, D-C5-X-verification-carrier) — the 4th carrier.

The fourth linking-carrier family (after Telegram's deep-link, Discord/Slack's OAuth-state,
and the phone OTP). C1 still owns the linking *lifecycle* + all its security (issue → redeem
→ bind, single-use, short-TTL, platform-bound, fail-closed); this is only the email carrier
around it, and it **reuses the phone channel's generic base32 OTP primitives verbatim** — the
code format + inline-redeem shape are identical, only the platform key + the copy differ:

- **Issue** a short base32 code (``LinkingService.issue`` with the ``generate_code``
  injection), shown to the user in the authenticated web app (C6).
- **Redeem inline over a plain email**: the user emails the code back **from the address to
  bind**; an *unlinked* inbound whose parsed body normalises to a code redeems through C1's
  unchanged ``redeem_and_bind``.

**The bound identity is the authenticity-validated ``From``** — the ``platform_identity`` the
inbound flow takes from the **B1-authenticated + B2-DMARC-verified** webhook payload, NEVER an
address parsed from the editable body (the C1-D-5 spoofing guard, identical to C4 binding the
signature-verified Twilio ``From``). A failed redeem is logged PII-safe (a salt-free
fingerprint, never the raw address). api-free.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from persona.logging import get_logger

# Reuse the phone channel's generic OTP-carrier primitives (the base32 code gen + the
# text→code normaliser + the redeem result shape) — the email carrier differs only in the
# platform key + copy, so duplicating them would be pure repetition.
from persona_connectors._phone.linking import (
    RedeemResult,
    RedeemStatus,
    generate_phone_code,
    normalise_phone_code,
)
from persona_connectors.email.connector import PLATFORM
from persona_connectors.errors import LinkTokenInvalidError

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from persona_connectors.domain.linking import LinkingService

__all__ = ["EmailLinkingService"]

_log = get_logger("connectors.email_linking")

_LINKED_MESSAGE = "You're linked! Just email a persona by name (or its address) to start."
_FAILED_MESSAGE = (
    "That code didn't work — it may have expired or already been used. Generate a fresh "
    "code from your Open Persona settings and email it back."
)


def _fingerprint(identity: str) -> str:
    """A short one-way fingerprint of an email address — for correlation, never PII."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


class EmailLinkingService:
    """The email carrier around C1's :class:`LinkingService` (issue code / redeem emailed code).

    Holds no state beyond its injected C1 ``LinkingService``. Token generation, single-use
    consumption, TTL, and the bind all live in C1 — this supplies only the email OTP carrier.
    """

    def __init__(self, *, linking: LinkingService) -> None:
        self._linking = linking

    def issue_code(self, *, owner_id: str, now: datetime, ttl: timedelta) -> str:
        """Issue a one-time code for ``owner_id``; return the emailable code (shown in the app)."""
        return self._linking.issue(
            owner_id=owner_id,
            platform=PLATFORM,
            now=now,
            ttl=ttl,
            generate_code=generate_phone_code,
        )

    def redeem_emailed_code(
        self, *, text: str, platform_identity: str, now: datetime
    ) -> RedeemResult:
        """Handle an inbound email body that may be an emailed-back code (the flow entry).

        ``platform_identity`` MUST be the ``From`` from the **B1+B2-validated** webhook —
        never parsed from ``text``. ``not_a_link_attempt`` for a non-code body (the flow sends
        the link-instruction), ``linked`` (+ owner_id + confirmation) on a valid redeem, or
        ``failed`` (+ retry copy, PII-safe log) when rejected — never a partial bind.
        """
        code = normalise_phone_code(text)
        if code is None:
            return RedeemResult(status=RedeemStatus.not_a_link_attempt)
        try:
            owner_id = self._linking.redeem_and_bind(
                plaintext_token=code,
                platform=PLATFORM,
                platform_identity=platform_identity,
                now=now,
            )
        except LinkTokenInvalidError:
            _log.warning(
                "email OTP redeem rejected (identity_fp={fp})", fp=_fingerprint(platform_identity)
            )
            return RedeemResult(status=RedeemStatus.failed, message=_FAILED_MESSAGE)
        return RedeemResult(status=RedeemStatus.linked, owner_id=owner_id, message=_LINKED_MESSAGE)

    def resolve_owner(self, *, platform_identity: str) -> str:
        """Resolve a (validated) email address to its linked owner via C1 (or raise)."""
        return self._linking.resolve_owner(platform=PLATFORM, platform_identity=platform_identity)
