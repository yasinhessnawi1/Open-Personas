"""R8 T1 — PKCE + `state` primitive contract tests (security-load-bearing).

PKCE (RFC 7636 / OAuth 2.1 §7.5.2) binds an authorization code to the client that
started the flow: the ``code_challenge`` travels on the authorize request, the
``code_verifier`` on the token exchange, and the AS recomputes
``BASE64URL(SHA256(verifier))`` and rejects a mismatch. A stolen code without the
verifier is useless. ``state`` (RFC 9700 / OAuth 2.1) is the opaque high-entropy
CSRF token. These are pure functions — the whole flow's integrity rests on them,
so their contract is pinned here before anything consumes them.
"""

from __future__ import annotations

import base64
import hashlib
import re

from persona_api.mcp.oauth.pkce import generate_pkce_pair, generate_state

# RFC 7636 §4.1 unreserved set for the code_verifier.
_UNRESERVED = re.compile(r"^[A-Za-z0-9\-._~]+$")


class TestPkcePair:
    def test_verifier_length_within_rfc7636_bounds(self) -> None:
        pair = generate_pkce_pair()
        assert 43 <= len(pair.verifier) <= 128

    def test_verifier_is_unreserved_charset_only(self) -> None:
        pair = generate_pkce_pair()
        assert _UNRESERVED.match(pair.verifier), "verifier must be RFC 7636 unreserved chars"

    def test_method_is_s256(self) -> None:
        assert generate_pkce_pair().method == "S256"

    def test_challenge_is_base64url_nopad_sha256_of_verifier(self) -> None:
        pair = generate_pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(pair.verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        assert pair.challenge == expected

    def test_challenge_has_no_base64_padding(self) -> None:
        # '=' padding in a challenge is a common interop break; S256 challenges are unpadded.
        assert "=" not in generate_pkce_pair().challenge

    def test_pairs_are_unpredictable(self) -> None:
        verifiers = {generate_pkce_pair().verifier for _ in range(50)}
        assert len(verifiers) == 50, "verifier must be freshly random per call"


class TestState:
    def test_state_is_high_entropy_and_urlsafe(self) -> None:
        state = generate_state()
        # token_urlsafe(32) → ≥43 chars of url-safe base64 (≥256 bits of entropy).
        assert len(state) >= 43
        assert re.match(r"^[A-Za-z0-9\-_]+$", state)

    def test_states_are_unique(self) -> None:
        states = {generate_state() for _ in range(100)}
        assert len(states) == 100
