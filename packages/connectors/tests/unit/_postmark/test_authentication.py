"""Sender authenticity — B2, the second anti-spoofing layer (Spec C5, D-C5-5, R9-072).

B2 decides whether a B1-authenticated inbound email's ``From`` is genuine.
``parse_authentication_results`` / ``sender_is_authentic`` cover the RFC 8601
``Authentication-Results`` header path — kept for the header's benefit *if it is ever
present*, but Postmark's real inbound-parse payload never sends one (see the module
docstring). ``sender_is_authentic_from_headers`` is the gate actually wired into the email
flow: it honours ``Authentication-Results`` first when present, and otherwise falls back to
Postmark's real aligned signal — SpamAssassin's ``DKIM_VALID_AU`` token in ``X-Spam-Tests``
(a valid DKIM signature aligned with the ``From:``/Author domain — DMARC's DKIM leg, computed
outside an ``Authentication-Results`` header). SPF alone (``SPF_PASS`` / ``Received-SPF``) is
deliberately never sufficient in either path, because it authenticates the envelope sender,
not ``From:``.

**The B1→B2 trust chain (load-bearing, unchanged by R9-072):** B2 trusts these headers ONLY
because B1 already proved the payload is genuinely Postmark's — a forged POST could write
``dmarc=pass`` (or ``X-Spam-Tests: DKIM_VALID_AU``) itself. So the order is asserted via the
negative here: a payload carrying a valid ``dmarc=pass`` is STILL rejected when B1 fails, and
the verdict is never even consulted.
"""

from __future__ import annotations

import pytest
from persona_connectors._postmark.authentication import (
    parse_authentication_results,
    parse_received_spf_verdict,
    parse_spam_test_tokens,
    sender_is_authentic,
    sender_is_authentic_from_headers,
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

# Postmark's OWN documented example X-Spam-Tests value (postmarkapp.com/developer/user-guide/
# inbound/parse-an-email) — the real-world pass case: DKIM signed, valid, AND aligned.
_SPAM_TESTS_ALIGNED = "DKIM_SIGNED,DKIM_VALID,DKIM_VALID_AU,SPF_PASS"
# Valid + signed DKIM, but NOT aligned with From: (no _AU) — the key security case.
_SPAM_TESTS_UNALIGNED = "DKIM_SIGNED,DKIM_VALID,SPF_PASS"
_SPAM_TESTS_SPF_ONLY = "SPF_PASS"
_RECEIVED_SPF_PASS = (
    "Pass (mx.postmark.com: domain of sender.com designates 1.2.3.4 as permitted sender)"
)

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


# --- the real Postmark gate: X-Spam-Tests' DKIM_VALID_AU (R9-072) ---


def test_spam_test_tokens_are_parsed_as_whole_tokens() -> None:
    tokens = parse_spam_test_tokens(_SPAM_TESTS_ALIGNED)
    assert tokens == {"DKIM_SIGNED", "DKIM_VALID", "DKIM_VALID_AU", "SPF_PASS"}


def test_received_spf_verdict_is_the_leading_word_lowercased() -> None:
    assert parse_received_spf_verdict(_RECEIVED_SPF_PASS) == "pass"
    assert parse_received_spf_verdict(None) is None


def test_postmarks_real_aligned_dkim_signal_is_authentic() -> None:
    """Postmark's own documented example X-Spam-Tests value: DKIM_VALID_AU present, no
    Authentication-Results header at all (the real-world pass case)."""
    headers = {"authentication-results": None, "x-spam-tests": _SPAM_TESTS_ALIGNED}
    assert sender_is_authentic_from_headers(headers) is True


def test_dkim_valid_without_au_alignment_is_not_authentic() -> None:
    """A validly-signed DKIM (DKIM_VALID) that is NOT aligned with From: (no _AU) must be
    rejected — this is the key security case DMARC alignment exists to catch, and proves
    DKIM_VALID is never substring-matched into DKIM_VALID_AU."""
    headers = {"authentication-results": None, "x-spam-tests": _SPAM_TESTS_UNALIGNED}
    assert sender_is_authentic_from_headers(headers) is False


def test_spf_pass_alone_is_not_authentic() -> None:
    """SPF authenticates the envelope, not From: — never sufficient on its own."""
    headers = {"authentication-results": None, "x-spam-tests": _SPAM_TESTS_SPF_ONLY}
    assert sender_is_authentic_from_headers(headers) is False


def test_no_spam_tests_header_at_all_is_rejected_fail_closed() -> None:
    headers: dict[str, str | None] = {"authentication-results": None, "x-spam-tests": None}
    assert sender_is_authentic_from_headers(headers) is False


def test_authentication_results_dmarc_pass_still_works_legacy_path() -> None:
    """If Authentication-Results IS ever present with dmarc=pass, it's honoured directly —
    no dependency on X-Spam-Tests at all."""
    headers = {"authentication-results": _PASS, "x-spam-tests": None}
    assert sender_is_authentic_from_headers(headers) is True


def test_explicit_dmarc_fail_overrides_a_present_dkim_valid_au() -> None:
    """Explicit ruling (R9-072): when Authentication-Results states an EXPLICIT DMARC result,
    that MTA-computed verdict is authoritative and wins over SpamAssassin's DKIM_VALID_AU
    heuristic in both directions — including an explicit fail overriding a present
    DKIM_VALID_AU. Rationale: Authentication-Results reflects the domain's actual published
    DMARC policy (including strict alignment modes); DKIM_VALID_AU is an approximation of
    one leg of that computation, without seeing the policy, so an explicit MTA failure is
    treated as more informative than the heuristic and refuses the sender."""
    headers = {"authentication-results": _DMARC_FAIL, "x-spam-tests": _SPAM_TESTS_ALIGNED}
    assert sender_is_authentic_from_headers(headers) is False


def test_dmarc_none_with_no_spam_tests_signal_is_still_rejected() -> None:
    """dmarc=none is an explicit (if weak) DMARC opinion — still authoritative, still refuses,
    even with no X-Spam-Tests fallback available."""
    headers = {"authentication-results": _DMARC_NONE, "x-spam-tests": None}
    assert sender_is_authentic_from_headers(headers) is False


def test_authentication_results_present_but_no_dmarc_token_falls_back_to_spam_tests() -> None:
    """Authentication-Results present (e.g. only dkim=/spf= stamped, no dmarc= at all) has NO
    DMARC opinion to be authoritative about — falls through to the X-Spam-Tests signal."""
    headers = {"authentication-results": _NO_DMARC_FIELD, "x-spam-tests": _SPAM_TESTS_ALIGNED}
    assert sender_is_authentic_from_headers(headers) is True


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
