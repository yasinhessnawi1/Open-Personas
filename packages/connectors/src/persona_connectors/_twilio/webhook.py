"""Twilio webhook signature validation (Spec C4 T3) — the mandatory security gate.

A Twilio webhook (inbound message OR status callback) is a **public endpoint** whose
only authentication is the ``X-Twilio-Signature`` header Twilio computes over the
request. So this check is the whole defence against spoofed "Twilio" POSTs (the
webhook-security risk, mirroring the Telegram D-C2-2 posture). Twilio's scheme:

1. Build the signature base = the full request **URL** with the POST params **sorted
   by key** and concatenated as ``key + value`` appended to the URL.
2. HMAC-**SHA1** that base with the account auth token; base64-encode.
3. **Constant-time compare** (:func:`hmac.compare_digest`) against the presented
   ``X-Twilio-Signature`` header.

Three load-bearing properties:

- **Constant-time** — compared with :func:`hmac.compare_digest`, never ``==`` (a plain
  compare leaks the expected signature a byte at a time via timing).
- **Validate-before-parse** — the CALLER contract: run this BEFORE acting on the
  parsed form, so unauthenticated input never drives a turn / a reply.
- **Fail-closed** — an unset token (none configured) OR a missing signature header
  rejects EVERY request rather than falling open. A public endpoint with no auth that
  accepts everything is the exact hole this gate exists to prevent.

Pure + api-free.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import SecretStr

__all__ = ["TWILIO_SIGNATURE_HEADER", "verify_twilio_signature"]

# The header Twilio sends the request signature in on every webhook POST.
TWILIO_SIGNATURE_HEADER = "X-Twilio-Signature"


def _expected_signature(token: str, url: str, params: Mapping[str, str]) -> str:
    """Compute Twilio's signature for a request (URL + sorted key+value params, HMAC-SHA1)."""
    base = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(token.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def verify_twilio_signature(
    auth_token: SecretStr | None,
    url: str,
    params: Mapping[str, str],
    signature: str | None,
) -> bool:
    """Whether a Twilio webhook request's ``X-Twilio-Signature`` is authentic (T3).

    Args:
        auth_token: The Twilio token the webhook is signed with (a ``SecretStr``), or
            ``None`` if none is configured.
        url: The full request URL Twilio signed (scheme + host + path [+ query]).
        params: The POST form params (Twilio sorts them by key before hashing — the
            caller dict order is irrelevant).
        signature: The value of the ``X-Twilio-Signature`` header, or ``None`` if absent.

    Returns:
        ``True`` only when a token IS configured AND the presented signature matches the
        computed one (constant-time). **Fail-closed:** an unset token (``auth_token is
        None``) or an absent signature returns ``False`` — the endpoint rejects rather
        than accepting unauthenticated traffic. **Validate-before-parse** is the caller
        contract: run this before acting on ``params``.
    """
    if auth_token is None or signature is None:
        return False
    expected = _expected_signature(auth_token.get_secret_value(), url, params)
    return hmac.compare_digest(signature, expected)
