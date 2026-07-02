"""Sender authenticity — B2, the second anti-spoofing layer (Spec C5, D-C5-5).

Reads the DKIM/SPF/DMARC verdict Postmark's inbound MTA stamps into the RFC 8601
``Authentication-Results`` header and decides whether the ``From`` is authentic. We do
**not** re-implement DKIM — we trust the ESP-computed verdict, and only because B1
(:mod:`_postmark.webhook`) already proved the payload is genuinely Postmark's (a forged
POST could otherwise write ``dmarc=pass`` itself; that is the B1→B2 chain).

**The gate is DMARC=pass.** DMARC is the one standard that protects the ``From`` header
(SPF authenticates the envelope sender, DKIM alone can be the spoofer's own aligned
domain — neither stops ``From: victim@`` without DMARC alignment). So ``dmarc=pass`` is the
single verdict that means "this From is authentic". **Fail-closed:** a missing header, a
missing/``none``/``fail`` DMARC result, or an unparseable header all read as **unverified**
→ the caller grants zero access (the same end-state as an unlinked identity). Broadening to
aligned-DKIM for non-DMARC senders is a documented follow-up, not a v1 silent-allow.

Owned surface — api-free; stdlib + pydantic only.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

__all__ = ["AuthResults", "parse_authentication_results", "sender_is_authentic"]

# Each method appears as ``method=result`` (RFC 8601); capture the alphabetic result
# token (``pass``/``fail``/``none``/``softfail``/…) at a word boundary, case-insensitive.
_DKIM = re.compile(r"\bdkim\s*=\s*([a-z]+)", re.IGNORECASE)
_SPF = re.compile(r"\bspf\s*=\s*([a-z]+)", re.IGNORECASE)
_DMARC = re.compile(r"\bdmarc\s*=\s*([a-z]+)", re.IGNORECASE)


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
    """Whether the sender's ``From`` is authentic per the ESP verdict (B2, fail-closed).

    ``True`` only when the DMARC result is exactly ``pass``. Everything else — a missing
    header, no DMARC result, ``dmarc=none``/``fail``/``temperror``/``permerror``, or an
    unparseable header — is **unverified** (``False``): the caller then denies access (zero
    access), never a bind/turn on a spoofable ``From``. **Trust precondition:** call this
    only on a B1-authenticated payload (:func:`_postmark.webhook.guard_inbound` enforces the
    order) — the verdict is Postmark's, meaningful only when the payload is Postmark's.
    """
    return parse_authentication_results(authentication_results).dmarc == "pass"
