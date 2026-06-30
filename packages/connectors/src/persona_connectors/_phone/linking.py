"""Phone-number OTP account linking (Spec C4 T7, D-C4-5) — the carrier, not the lifecycle.

The **third** linking carrier family (after Telegram's deep-link and Discord/Slack's
OAuth-state — C1-D-5 always named OTP as the third). C1 still owns the linking
*lifecycle* and ALL its security (issue → redeem → bind, single-use, short-TTL,
platform-bound, fail-closed); this module is only the phone carrier around it:

- **Issue** a short **textable** code (8-char Crockford base32, ~40 bits) — handed
  to C1's :meth:`LinkingService.issue` via the ``generate_code`` injection
  (D-C4-X-c1-issue-codegen, T6), shown to the user in the authenticated web app.
- **Redeem inline over a plain message** (the Telegram ``/start`` shape, but with no
  URL): the user texts the code back; an *unlinked* inbound whose text normalises to
  a code is redeemed through C1's unchanged :meth:`LinkingService.redeem_and_bind`.

**The identity bound is the caller-supplied number** — the `platform_identity` the
inbound flow takes from the **signature-verified provider webhook** (Twilio's `From`),
NEVER a number parsed from the editable message text (the C1-D-5 spoofing guard).

**Entropy without a counter.** 8 Crockford chars = ``32**8 = 2**40`` (~1.1e12); with a
single-use code, a ~10-minute TTL, and inbound SMS being attacker-cost-bearing +
provider-rate-limited, brute force is infeasible without an attempts counter — so the
table/lifecycle stay untouched (D-C4-5). A **failed redeem is logged** (PII-safe — a
salt-free fingerprint, never the raw number) so probing is at least observable.

**api-free** — pure carrier logic over C1's owned-surface ``LinkingService`` +
persona-core logging; no ``persona_api``.
"""

from __future__ import annotations

import hashlib
import secrets
from enum import StrEnum
from typing import TYPE_CHECKING

from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict

from persona_connectors.errors import LinkTokenInvalidError

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from persona_connectors.domain.linking import LinkingService

__all__ = [
    "PhoneLinkingService",
    "RedeemResult",
    "RedeemStatus",
    "generate_phone_code",
    "normalise_phone_code",
]

_log = get_logger("connectors.phone_linking")

# Crockford base32 — excludes I, L, O, U to kill human transcription ambiguity.
# 8 chars → 32**8 = 2**40 (~1.1e12) of entropy.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_SET = frozenset(_CROCKFORD)
_CODE_LEN = 8
# The confusions Crockford decoding folds away on input (case-insensitive already).
_CONFUSIONS = {"I": "1", "L": "1", "O": "0"}

_LINKED_MESSAGE = "You're linked! Just message a persona by name to start."
_FAILED_MESSAGE = (
    "That code didn't work — it may have expired or already been used. "
    "Generate a fresh code from your Open Persona settings and text it back."
)


def generate_phone_code() -> str:
    """Return a fresh 8-char Crockford-base32 code (~40 bits, transcription-safe)."""
    return "".join(secrets.choice(_CROCKFORD) for _ in range(_CODE_LEN))


def normalise_phone_code(text: str) -> str | None:
    """Normalise a texted-back code to its canonical form, or ``None`` if not a code.

    Folds case, strips spaces/hyphens, and applies the Crockford input confusions
    (``O→0``, ``I/L→1``). Returns the 8-char canonical code only when the whole
    (cleaned) string is exactly 8 Crockford characters — otherwise ``None`` (a normal
    message, not a redeem attempt). This is the cheap pre-filter before hashing.
    """
    cleaned = "".join(text.upper().replace("-", "").split())
    folded = "".join(_CONFUSIONS.get(ch, ch) for ch in cleaned)
    if len(folded) == _CODE_LEN and all(ch in _CROCKFORD_SET for ch in folded):
        return folded
    return None


def _fingerprint(identity: str) -> str:
    """A short one-way fingerprint of a phone number — for correlation, never PII."""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]


class RedeemStatus(StrEnum):
    """The outcome of an inbound message treated as a possible OTP redeem.

    Values:
        linked: A valid code redeemed — the phone number is now bound to its owner.
        failed: A code-shaped message was presented but rejected (expired / used /
            unknown) — fail-closed, no bind.
        not_a_link_attempt: The text is not a code (a normal message) — the flow
            handles it normally (the link-instruction for an unlinked sender).
    """

    linked = "linked"
    failed = "failed"
    not_a_link_attempt = "not_a_link_attempt"


class RedeemResult(BaseModel):
    """The result of attempting to redeem an inbound message as an OTP code.

    Attributes:
        status: The :class:`RedeemStatus`.
        owner_id: The bound owner on ``linked`` (so the flow can greet/foreground);
            ``None`` otherwise.
        message: The product-voice reply to send (``None`` for ``not_a_link_attempt``
            — the flow composes that path's reply).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: RedeemStatus
    owner_id: str | None = None
    message: str | None = None


class PhoneLinkingService:
    """The phone carrier around C1's :class:`LinkingService` (issue code / redeem).

    Holds no state beyond its injected C1 ``LinkingService`` + the platform key
    (``"whatsapp"`` / ``"sms"``). Token generation, single-use consumption, TTL, and
    the bind all live in C1 — this only supplies the OTP carrier (code format +
    inline-text redeem). One instance per platform; the two phone adapters reuse it.
    """

    def __init__(self, *, linking: LinkingService, platform: str) -> None:
        self._linking = linking
        self._platform = platform

    def issue_code(self, *, owner_id: str, now: datetime, ttl: timedelta) -> str:
        """Issue a one-time OTP code for ``owner_id``; return the textable code.

        Delegates to C1's ``issue`` with the base32 generator (T6): C1 stores only
        the code's hash (single-use, TTL'd). The plaintext code is shown to the user
        in the authenticated web app; it is only ever carried back over the message.
        """
        return self._linking.issue(
            owner_id=owner_id,
            platform=self._platform,
            now=now,
            ttl=ttl,
            generate_code=generate_phone_code,
        )

    def redeem_texted_code(
        self, *, text: str, platform_identity: str, now: datetime
    ) -> RedeemResult:
        """Handle an inbound message that may be a texted-back OTP code (the flow entry).

        ``platform_identity`` MUST be the phone number from the **signature-verified
        webhook** — never parsed from ``text`` (which is user-editable). Returns
        ``not_a_link_attempt`` for a non-code message (the flow sends the
        link-instruction), ``linked`` (+ owner_id + confirmation) on a valid redeem,
        or ``failed`` (+ friendly retry copy, and a PII-safe log) when rejected —
        never a partial bind (C1 raises before binding on any violation).
        """
        code = normalise_phone_code(text)
        if code is None:
            return RedeemResult(status=RedeemStatus.not_a_link_attempt)
        try:
            owner_id = self._linking.redeem_and_bind(
                plaintext_token=code,
                platform=self._platform,
                platform_identity=platform_identity,
                now=now,
            )
        except LinkTokenInvalidError:
            _log.warning(
                "phone OTP redeem rejected (platform={platform} identity_fp={fp})",
                platform=self._platform,
                fp=_fingerprint(platform_identity),
            )
            return RedeemResult(status=RedeemStatus.failed, message=_FAILED_MESSAGE)
        return RedeemResult(status=RedeemStatus.linked, owner_id=owner_id, message=_LINKED_MESSAGE)

    def resolve_owner(self, *, platform_identity: str) -> str:
        """Resolve a (verified) phone number to its linked owner via C1 (or raise).

        Thin pass-through to C1's ``resolve_owner`` — the inbound flow uses it after
        linking; an unlinked number raises ``IdentityNotLinkedError`` (zero access).
        """
        return self._linking.resolve_owner(
            platform=self._platform, platform_identity=platform_identity
        )
