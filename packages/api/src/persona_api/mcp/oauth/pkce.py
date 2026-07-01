"""PKCE (RFC 7636) + ``state`` (RFC 9700) primitives — R8 T1.

Pure, dependency-free crypto over the stdlib (``secrets`` + ``hashlib``), per the
research §5 ruling: no OAuth-client library, we own this narrow security-reviewed
subset. Two primitives:

- :func:`generate_pkce_pair` — a fresh ``code_verifier`` + its S256
  ``code_challenge``. OAuth 2.1 §7.5.2 makes PKCE S256 mandatory on the code
  exchange; the challenge rides the authorize request, the verifier the token
  exchange, and the AS rejects a mismatch (code-injection defence).
- :func:`generate_state` — an opaque high-entropy CSRF token bound server-side to
  the flow (never carrying the redirect target or a secret — R8-D-6).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass

__all__ = ["PkcePair", "generate_pkce_pair", "generate_state"]


def _b64url_nopad(raw: bytes) -> str:
    """Base64url-encode ``raw`` with no ``=`` padding (the OAuth/JOSE convention)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class PkcePair:
    """A PKCE ``code_verifier`` and its derived S256 ``code_challenge``.

    Frozen: the pair is minted once and never mutated. ``verifier`` is the secret
    (sent only on the back-channel token exchange, never in a browser-visible URL);
    ``challenge`` is public (rides the authorize redirect).
    """

    verifier: str
    challenge: str
    method: str = "S256"


def generate_pkce_pair() -> PkcePair:
    """Return a fresh :class:`PkcePair` (S256).

    ``secrets.token_urlsafe(32)`` yields ~43 chars from the RFC 7636 unreserved
    subset (``A-Za-z0-9-_``) — 256 bits of entropy, within the 43–128 length
    bound. The challenge is ``BASE64URL-NOPAD(SHA256(ASCII(verifier)))``.
    """
    verifier = secrets.token_urlsafe(32)
    challenge = _b64url_nopad(hashlib.sha256(verifier.encode("ascii")).digest())
    return PkcePair(verifier=verifier, challenge=challenge)


def generate_state() -> str:
    """Return an opaque high-entropy ``state`` (≥256 bits, url-safe).

    Mapped server-side to (owner_id, server_id, PKCE verifier, redirect target);
    the value itself is meaningless — never encode anything in it (R8-D-6).
    """
    return secrets.token_urlsafe(32)
