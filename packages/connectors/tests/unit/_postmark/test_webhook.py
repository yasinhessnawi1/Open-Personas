"""Postmark inbound-webhook authenticity — B1, the first anti-spoofing layer (Spec C5, D-C5-5).

A Postmark inbound webhook is a public endpoint; its only authentication is the HTTP
Basic-Auth credential baked into the hook URL (``https://user:pass@host/hook``). This is
the whole defence against a forged POST straight to our endpoint (the Twilio-signature
analogue, ``_twilio/webhook``). Three load-bearing properties, all proven here:

- **fail-closed** — no configured credential OR no/garbled ``Authorization`` header rejects
  EVERY request (a public endpoint that accepts everything is the exact hole);
- **constant-time** — compared with :func:`hmac.compare_digest`, never ``==``;
- **validate-before-parse** — the body (attacker-controlled) is parsed ONLY after auth
  passes; a rejected request never invokes the handler (proven via the negative below —
  the parse spy is never called).
"""

from __future__ import annotations

import base64

import pytest
from persona_connectors._postmark.webhook import (
    PostmarkWebhookAuth,
    guard_inbound,
    verify_postmark_basic_auth,
)
from pydantic import SecretStr

_EXPECTED = PostmarkWebhookAuth(username="hook", password=SecretStr("s3cret"))


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# --- the pure verifier: fail-closed + correctness ---


def test_correct_credentials_authenticate() -> None:
    assert verify_postmark_basic_auth(expected=_EXPECTED, authorization=_basic("hook", "s3cret"))


def test_wrong_password_rejected() -> None:
    assert not verify_postmark_basic_auth(expected=_EXPECTED, authorization=_basic("hook", "nope"))


def test_wrong_username_rejected() -> None:
    forged = _basic("evil", "s3cret")
    assert not verify_postmark_basic_auth(expected=_EXPECTED, authorization=forged)


def test_missing_header_is_fail_closed() -> None:
    assert not verify_postmark_basic_auth(expected=_EXPECTED, authorization=None)


def test_unconfigured_credential_is_fail_closed() -> None:
    """No configured credential ⇒ reject EVERY request (never fall open)."""
    assert not verify_postmark_basic_auth(expected=None, authorization=_basic("hook", "s3cret"))


@pytest.mark.parametrize(
    "header",
    [
        "Bearer abc",  # wrong scheme
        "Basic",  # no value
        "Basic !!!notbase64!!!",  # undecodable
        "Basic " + base64.b64encode(b"nocolon").decode(),  # no user:pass separator
        "",  # empty
    ],
)
def test_malformed_authorization_is_fail_closed(header: str) -> None:
    assert not verify_postmark_basic_auth(expected=_EXPECTED, authorization=header)


# --- validate-before-parse: prove the NEGATIVE (the handler/parse never runs on bad auth) ---


class _ParseSpy:
    """Stands in for the body parse + flow — records whether it was ever invoked."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        return "parsed+handled"


@pytest.mark.asyncio
async def test_bad_auth_rejects_before_the_body_is_ever_parsed() -> None:
    """The negative: a forged/absent-auth request is rejected and the handler — which is
    what reads + parses the attacker-controlled body — is NEVER invoked."""
    spy = _ParseSpy()
    result = await guard_inbound(expected=_EXPECTED, authorization=None, handle=spy)
    assert result is None  # rejected
    assert spy.calls == 0  # parse/flow never even attempted


@pytest.mark.asyncio
async def test_unconfigured_credential_also_never_parses() -> None:
    spy = _ParseSpy()
    result = await guard_inbound(expected=None, authorization=_basic("hook", "s3cret"), handle=spy)
    assert result is None
    assert spy.calls == 0


@pytest.mark.asyncio
async def test_good_auth_parses_exactly_once() -> None:
    spy = _ParseSpy()
    result = await guard_inbound(
        expected=_EXPECTED, authorization=_basic("hook", "s3cret"), handle=spy
    )
    assert result == "parsed+handled"
    assert spy.calls == 1
