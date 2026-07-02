"""Postmark inbound-webhook authenticity — B1, the first anti-spoofing layer (Spec C5, D-C5-5).

A Postmark inbound webhook is a **public endpoint**; its only authentication is the HTTP
Basic-Auth credential configured on the hook URL (``https://user:pass@host/hook``), which
Postmark then presents on every POST. This module is the whole defence against a forged
POST straight to our endpoint (the Twilio-signature analogue, :mod:`_twilio.webhook`):

- **fail-closed** — an unset credential (``expected is None``) OR a missing/garbled
  ``Authorization`` header rejects EVERY request. A public endpoint with no auth that
  accepts everything is the exact hole this gate exists to close.
- **constant-time** — the username + password are compared with :func:`hmac.compare_digest`,
  never ``==`` (a plain compare leaks the secret a byte at a time via timing); both fields
  are always compared (no early-out) so a username miss and a password miss are
  indistinguishable.
- **validate-before-parse** — :func:`guard_inbound` authenticates FIRST and invokes the
  handler (which reads + parses the attacker-controlled body) only on success. This is the
  first half of the B1→B2 chain: B2 trusts Postmark's ``Authentication-Results`` verdict
  *because* B1 proved the payload is genuinely Postmark's.

Owned surface — api-free (stdlib + pydantic only); the ASGI route (Group E) calls in here.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, ConfigDict, SecretStr

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["PostmarkWebhookAuth", "guard_inbound", "verify_postmark_basic_auth"]

_T = TypeVar("_T")


class PostmarkWebhookAuth(BaseModel):
    """The Basic-Auth credential the Postmark inbound webhook is configured with.

    ``password`` is a :class:`~pydantic.SecretStr` so it never lands in a log/repr; the
    plaintext is read only inside the constant-time compare.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    username: str
    password: SecretStr


def verify_postmark_basic_auth(
    *, expected: PostmarkWebhookAuth | None, authorization: str | None
) -> bool:
    """Whether an inbound webhook's ``Authorization`` header is authentic (fail-closed).

    Returns ``True`` only when a credential IS configured AND the presented Basic-Auth
    username+password both match (constant-time). **Fail-closed:** an unset ``expected``,
    an absent/empty header, a non-Basic scheme, undecodable base64, or a missing ``:``
    separator all return ``False`` — the endpoint rejects rather than accept unauthenticated
    traffic. **Validate-before-parse** is the caller contract (use :func:`guard_inbound`).
    """
    if expected is None or not authorization:
        return False
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, ValueError):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    # Compare BOTH fields always (assigned before the combine — no early-out), so a
    # username miss and a password miss take the same time.
    username_ok = hmac.compare_digest(username, expected.username)
    password_ok = hmac.compare_digest(password, expected.password.get_secret_value())
    return username_ok and password_ok


async def guard_inbound(
    *,
    expected: PostmarkWebhookAuth | None,
    authorization: str | None,
    handle: Callable[[], Awaitable[_T]],
) -> _T | None:
    """Authenticate the webhook, then — only on success — invoke ``handle`` (B1).

    **Validate-before-parse:** on an auth failure this returns ``None`` and ``handle`` —
    the callable that reads + parses the attacker-controlled body and drives the flow — is
    **never invoked**. On success it returns ``handle()``'s result. The ASGI route (Group E)
    passes ``handle`` as a closure over the request so the body is touched strictly after
    auth passes.
    """
    if not verify_postmark_basic_auth(expected=expected, authorization=authorization):
        return None
    return await handle()
