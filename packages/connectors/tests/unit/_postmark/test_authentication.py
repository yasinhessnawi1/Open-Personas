"""Sender authenticity — B2, the second anti-spoofing layer (Spec C5, D-C5-5).

B2 reads the DKIM/SPF/DMARC verdict Postmark's inbound MTA stamps into the
``Authentication-Results`` header (RFC 8601) and rejects a spoofed ``From`` before any
identity resolution. The gate is **DMARC=pass**: DMARC is the standard that protects the
``From`` header (SPF checks the envelope sender, DKIM alone can be a spoofer's own aligned
domain — neither stops ``From: victim@`` without DMARC alignment), so it's the single
verdict that reliably means "this From is authentic".

**The B1→B2 trust chain (load-bearing):** B2 trusts this header ONLY because B1 already
proved the payload is genuinely Postmark's — a forged POST could write ``dmarc=pass``
itself. So the order is asserted via the negative here: a payload carrying a valid
``dmarc=pass`` is STILL rejected when B1 fails, and the verdict is never even consulted.
"""

from __future__ import annotations

import pytest
from persona_connectors._postmark.authentication import (
    parse_authentication_results,
    sender_is_authentic,
)
from persona_connectors._postmark.webhook import PostmarkWebhookAuth, guard_inbound
from pydantic import SecretStr

# Realistic Postmark-stamped headers.
_PASS = (
    "mx.postmark.com; dkim=pass header.i=@sender.com header.s=google header.b=abc; "
    "spf=pass smtp.mailfrom=sender.com; dmarc=pass (p=REJECT sp=REJECT dis=NONE)"
)
_DMARC_FAIL = "mx.postmark.com; dkim=fail; spf=softfail; dmarc=fail (p=REJECT)"
_DMARC_NONE = "mx.postmark.com; dkim=pass header.i=@x.com; spf=pass; dmarc=none"
_NO_DMARC_FIELD = "mx.postmark.com; dkim=pass; spf=pass"

_EXPECTED = PostmarkWebhookAuth(username="hook", password=SecretStr("s3cret"))


# --- the verdict parser + the fail-closed gate (both directions) ---


def test_parses_all_three_method_results() -> None:
    result = parse_authentication_results(_PASS)
    assert result.dkim == "pass"
    assert result.spf == "pass"
    assert result.dmarc == "pass"


def test_dmarc_pass_is_authentic() -> None:
    assert sender_is_authentic(_PASS) is True


def test_dmarc_fail_is_rejected() -> None:
    assert sender_is_authentic(_DMARC_FAIL) is False


def test_dmarc_none_is_rejected() -> None:
    """``dmarc=none`` (the From domain publishes no policy) → we can't verify From
    authenticity → fail-closed reject (the v1 conservative choice)."""
    assert sender_is_authentic(_DMARC_NONE) is False


def test_absent_dmarc_field_is_rejected() -> None:
    """A verdict present for DKIM/SPF but NO dmarc result → rejected (never infer pass
    from the weaker signals)."""
    assert sender_is_authentic(_NO_DMARC_FIELD) is False


def test_absent_or_empty_header_is_rejected() -> None:
    assert sender_is_authentic(None) is False
    assert sender_is_authentic("") is False


def test_malformed_header_is_rejected() -> None:
    assert sender_is_authentic("garbage with no methods at all") is False
    assert sender_is_authentic("dmarc=") is False  # no result token after '='


# --- the B1→B2 order, proven via the negative ---


@pytest.mark.asyncio
async def test_b1_failure_short_circuits_before_the_verdict_is_consulted() -> None:
    """The trust chain: a payload carrying a genuinely-passing ``dmarc=pass`` verdict is
    STILL rejected when B1 (webhook auth) fails — and B2 (the verdict) is NEVER consulted,
    because :func:`guard_inbound` short-circuits before the handler that would read it.
    A forged POST can't get its self-asserted ``dmarc=pass`` trusted."""
    consulted: list[bool] = []

    async def handle() -> bool:
        verdict = sender_is_authentic(_PASS)  # a real passing verdict on the (forged) body
        consulted.append(verdict)
        return verdict

    result = await guard_inbound(expected=_EXPECTED, authorization=None, handle=handle)  # B1 fails
    assert result is None  # rejected by B1
    assert consulted == []  # B2 never reached — the verdict was never parsed or trusted


@pytest.mark.asyncio
async def test_b1_pass_then_verdict_is_consulted() -> None:
    """The positive control: with B1 passing, the handler runs and B2 is consulted."""
    import base64

    consulted: list[bool] = []

    async def handle() -> bool:
        verdict = sender_is_authentic(_PASS)
        consulted.append(verdict)
        return verdict

    good = "Basic " + base64.b64encode(b"hook:s3cret").decode()
    result = await guard_inbound(expected=_EXPECTED, authorization=good, handle=handle)
    assert result is True
    assert consulted == [True]  # B2 reached only after B1 passed
