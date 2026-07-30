"""Sender authenticity — B2, the second anti-spoofing layer (Spec C5, D-C5-5, R9-072).

Decides whether the ``From`` header of a B1-authenticated inbound email is genuine. We do
**not** re-implement DKIM — we trust ESP-computed verdicts, and only because B1
(:mod:`_postmark.webhook`) already proved the payload is genuinely Postmark's (a forged
POST could otherwise self-assert any verdict it likes; that is the B1→B2 chain, and it is
UNCHANGED by this module: everything here is still meaningless on a payload B1 rejected).

**Reality check (R9-072):** Postmark's inbound-parse webhook does **not** emit an
``Authentication-Results`` header (RFC 8601) — its documented ``Headers`` array never
contains one (postmarkapp.com/developer/user-guide/inbound/parse-an-email). The original
``dmarc == "pass"`` gate below is kept for the header's benefit *if it is ever present*
(a future Postmark change, or a non-Postmark source that does stamp it), but on real
Postmark traffic it is always absent, so relying on it alone makes this gate permanently
closed and inbound email dead by construction. :func:`sender_is_authentic_from_headers` is
the gate actually wired into the flow (see :mod:`persona_connectors.email.flow`); it adds
the real Postmark-native aligned signal below.

**Why SPF alone is deliberately insufficient (do not "fix" this by accepting SPF_PASS).**
``Received-SPF`` / SpamAssassin's ``SPF_PASS`` report on the **envelope** sender
(``smtp.mailfrom=``), not the ``From:`` header. An attacker can register their own domain,
get a clean envelope SPF pass for it, and still write ``From: victim@example.com`` in the
message itself — the envelope and the visible ``From`` are independent fields. Accepting a
bare SPF pass as "this From is authentic" would let exactly that attack through, since
``sender_id`` (and therefore the bind + every subsequent turn) keys off ``From``. SPF is
parsed below **for diagnostics only** and must never by itself authorise a sender.

**Postmark's real aligned signal: SpamAssassin's ``DKIM_VALID_AU`` token**, carried in the
``X-Spam-Tests`` header. SpamAssassin's ``DKIM_VALID`` fires for *any* validly-signed DKIM
domain (no alignment with ``From:`` required — not sufficient on its own, same problem as
SPF). ``DKIM_VALID_AU`` fires only when the valid DKIM signature's domain is the **AUthor**
domain, i.e. aligned with ``From:`` — this is exactly DMARC's DKIM leg (RFC 7489 §3.1.1),
computed by SpamAssassin instead of stamped as ``Authentication-Results``. Accepting
``DKIM_VALID_AU`` is therefore not a loosening of the gate; it is recognising the same
alignment proof DMARC would have certified, expressed in the header Postmark actually sends.

**Fail-closed, unchanged:** a missing/unparseable ``Authentication-Results`` header, a
missing/``none``/explicit-``fail`` DMARC result with no compensating ``DKIM_VALID_AU``, a
missing ``X-Spam-Tests`` header, or ``DKIM_VALID``/``SPF_PASS`` without the ``_AU`` alignment
token all read as **unverified** → zero access, the same end-state as an unlinked identity.

**The ``dmarc=fail`` vs ``DKIM_VALID_AU`` conflict (explicit ruling, R9-072):** when an
``Authentication-Results`` header IS present and states an explicit DMARC result (anything
other than absent), that result is authoritative and short-circuits the SpamAssassin
heuristic in both directions — including an explicit ``fail`` overriding a present
``DKIM_VALID_AU``. Rationale: ``Authentication-Results`` is the MTA's own DMARC evaluation,
computed with full knowledge of the domain's published policy (including alignment mode,
``adkim``/``aspf``); ``DKIM_VALID_AU`` is a SpamAssassin heuristic approximating one leg of
that same computation from the signature alone, without seeing the policy. A domain can
publish DMARC alignment stricter than SpamAssassin's approximation accounts for (e.g.
``adkim=s`` strict alignment where SpamAssassin's looser domain match still fires
``_AU``), so an explicit MTA-computed failure is treated as strictly more informative than
the heuristic and wins. ``DKIM_VALID_AU`` is consulted only when ``Authentication-Results``
carries no DMARC opinion at all (the real Postmark case).

Owned surface — api-free; stdlib + pydantic only.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AuthResults",
    "parse_authentication_results",
    "parse_received_spf_verdict",
    "parse_spam_test_tokens",
    "sender_is_authentic",
    "sender_is_authentic_from_headers",
]

# Each method appears as ``method=result`` (RFC 8601); capture the alphabetic result
# token (``pass``/``fail``/``none``/``softfail``/…) at a word boundary, case-insensitive.
_DKIM = re.compile(r"\bdkim\s*=\s*([a-z]+)", re.IGNORECASE)
_SPF = re.compile(r"\bspf\s*=\s*([a-z]+)", re.IGNORECASE)
_DMARC = re.compile(r"\bdmarc\s*=\s*([a-z]+)", re.IGNORECASE)

# The SpamAssassin token that means "a valid DKIM signature aligned with the From/Author
# domain" — DMARC's DKIM leg, computed outside an Authentication-Results header. Matched as
# a whole token only (see parse_spam_test_tokens): never a substring match against the
# weaker ``DKIM_VALID`` (which lacks the ``_AU`` alignment suffix and is NOT sufficient).
_DKIM_VALID_AU = "DKIM_VALID_AU"

# Received-SPF's leading word is the verdict (e.g. "Pass (mx.postmark.com: domain of ...)").
# Diagnostics only — see the module docstring for why SPF never authorises a sender alone.
_RECEIVED_SPF_VERDICT = re.compile(r"^\s*([a-zA-Z]+)")


class AuthResults(BaseModel):
    """The three method verdicts extracted from ``Authentication-Results`` (``None`` if absent)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dkim: str | None = None
    spf: str | None = None
    dmarc: str | None = None


def _result(pattern: re.Pattern[str], header: str) -> str | None:
    match = pattern.search(header)
    return match.group(1).lower() if match else None


def parse_authentication_results(header: str | None) -> AuthResults:
    """Extract the DKIM/SPF/DMARC result tokens from an ``Authentication-Results`` header.

    Tolerant of the real header's noise (per-method properties like ``header.i=@d``,
    ``smtp.mailfrom=…``, and the DMARC ``(p=REJECT …)`` policy comment): each result is the
    alphabetic token immediately after ``method=``. An absent header yields all-``None``.
    """
    if not header:
        return AuthResults()
    return AuthResults(
        dkim=_result(_DKIM, header),
        spf=_result(_SPF, header),
        dmarc=_result(_DMARC, header),
    )


def sender_is_authentic(authentication_results: str | None) -> bool:
    """Whether the sender's ``From`` is authentic per an ``Authentication-Results`` verdict.

    ``True`` only when the DMARC result is exactly ``pass``. Everything else — a missing
    header, no DMARC result, ``dmarc=none``/``fail``/``temperror``/``permerror``, or an
    unparseable header — is **unverified** (``False``). **Kept for the header's benefit if
    it is ever present** (future Postmark change, or a non-Postmark source); on real Postmark
    traffic today this header is always absent (see the module docstring), so
    :func:`sender_is_authentic_from_headers` — not this function — is the gate actually wired
    into the email flow. **Trust precondition (unchanged):** call this only on a
    B1-authenticated payload (:func:`_postmark.webhook.guard_inbound` enforces the order) —
    the verdict is Postmark's, meaningful only when the payload is Postmark's.
    """
    return parse_authentication_results(authentication_results).dmarc == "pass"


def parse_spam_test_tokens(x_spam_tests: str | None) -> frozenset[str]:
    """The uppercased, whole-token set from a SpamAssassin ``X-Spam-Tests`` header.

    Postmark's documented format is a bare comma-separated token list (e.g.
    ``"DKIM_SIGNED,DKIM_VALID,DKIM_VALID_AU,SPF_PASS"``), with no per-token scores (those live
    in ``X-Spam-Status`` instead). Splitting on ``,`` and comparing whole tokens (never a
    substring search) is what keeps ``DKIM_VALID`` and ``DKIM_VALID_AU`` from being confused
    for each other in either direction. An absent/empty header yields an empty set.
    """
    if not x_spam_tests:
        return frozenset()
    return frozenset(token.strip().upper() for token in x_spam_tests.split(",") if token.strip())


def parse_received_spf_verdict(received_spf: str | None) -> str | None:
    """The leading verdict word of a ``Received-SPF`` header, lowercased (``None`` if absent).

    E.g. ``"Pass (mx.postmark.com: domain of ... designates ...)"`` → ``"pass"``.
    **Diagnostics only** — this reports the envelope sender's SPF result, not the ``From:``
    header's alignment, and must never by itself authorise a sender (see the module
    docstring: this is precisely the attack the gate exists to stop).
    """
    if not received_spf:
        return None
    match = _RECEIVED_SPF_VERDICT.match(received_spf)
    return match.group(1).lower() if match else None


def sender_is_authentic_from_headers(headers: Mapping[str, str | None]) -> bool:
    """Whether the ``From`` is authentic, per Postmark's real inbound headers (B2, R9-072).

    ``headers`` is the lowercased header-name → value map (see
    :func:`persona_connectors.email.inbound._headers_map`). ``True`` iff, in order:

    1. ``Authentication-Results`` carries an explicit DMARC result (present at all, not
       absent) — that result is authoritative: ``dmarc=pass`` → authentic; any other explicit
       result (``fail``/``none``/``temperror``/``permerror``/…) → **not** authentic, even if
       ``X-Spam-Tests`` carries ``DKIM_VALID_AU`` (see the module docstring's explicit ruling
       on this conflict: the MTA's own DMARC evaluation outranks SpamAssassin's heuristic).
    2. Otherwise (no ``Authentication-Results`` header, or present with no DMARC opinion — the
       real Postmark case) — authentic iff ``X-Spam-Tests`` contains the whole token
       ``DKIM_VALID_AU`` (DMARC's DKIM leg, aligned with ``From:``; see
       :func:`parse_spam_test_tokens`).

    Fail-closed in every other case: no ``X-Spam-Tests`` header at all, ``DKIM_VALID`` without
    the ``_AU`` alignment suffix, ``SPF_PASS``/``Received-SPF`` alone (never sufficient — see
    the module docstring), or any unparseable input. **Trust precondition (unchanged):** call
    only on a B1-authenticated payload — B1's Basic-Auth is what makes these headers
    Postmark's in the first place; nothing here re-derives that.
    """
    dmarc = parse_authentication_results(headers.get("authentication-results")).dmarc
    if dmarc is not None:
        return dmarc == "pass"
    return _DKIM_VALID_AU in parse_spam_test_tokens(headers.get("x-spam-tests"))
