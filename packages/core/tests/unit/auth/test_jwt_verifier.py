"""Unit tests for the extracted JWT verifier (spec V1 T03 / D-V1-X-jwt-verifier-extraction).

Mirrors the persona-api ``test_api_auth.py`` algorithm-confusion suite but
against the persona-core surface directly (no FastAPI). These tests guard the
contract persona-voice will consume (D-V1-1 branch (A) — `livekit-rtc` client
joins the LiveKit Room via a JWT signed by the user's IdP; persona-voice
verifies the token against the same ``make_jwt_verifier`` shape as persona-api,
sharing the algorithm-confusion-hardened key↔alg-family binding).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import pytest
from jose import jwt
from persona.auth import (
    AuthenticatedUser,
    JwtVerifierConfig,
    make_jwt_verifier,
)
from persona.errors import AuthenticationError
from pydantic import SecretStr, ValidationError


@dataclass(frozen=True)
class _Cfg:
    """Concrete settings object that satisfies ``JwtVerifierConfig`` structurally.

    Tests cover persona-core's surface directly — the API-side ``APIConfig``
    satisfies the same Protocol; both are exercised end-to-end through the
    persona-api auth test suite.
    """

    jwt_secret: SecretStr | None = None
    jwt_public_key: SecretStr | None = None
    jwt_algorithms_list: list[str] = ()  # type: ignore[assignment]
    jwt_audience: str | None = None


def _cfg(
    *,
    secret: str | None = None,
    public_key: str | None = None,
    algorithms: list[str] | None = None,
    audience: str | None = None,
) -> JwtVerifierConfig:
    return _Cfg(
        jwt_secret=SecretStr(secret) if secret else None,
        jwt_public_key=SecretStr(public_key) if public_key else None,
        # Distinguish None (caller-omitted; default to HS256) from [] (caller
        # explicitly empty; preserved so we can test the "no usable alg" gate).
        jwt_algorithms_list=list(algorithms) if algorithms is not None else ["HS256"],
        jwt_audience=audience,
    )


def test_authenticated_user_is_frozen_and_extra_forbid() -> None:
    user = AuthenticatedUser(id="u1", email="x@y.test")
    assert user.id == "u1"
    assert user.email == "x@y.test"
    # frozen + extra=forbid invariants — these are the boundary-type contract
    # downstream specs depend on (Pydantic v2 frozen + extra=forbid per D-05-9).
    with pytest.raises(ValidationError):
        AuthenticatedUser(id="u2", role="admin")  # type: ignore[call-arg]


def test_hs256_round_trip_resolves_sub_and_email() -> None:
    verify = make_jwt_verifier(_cfg(secret="s3cret", algorithms=["HS256"]))
    token = jwt.encode(
        {"sub": "u1", "email": "a@x.test", "exp": int(time.time()) + 60},
        "s3cret",
        algorithm="HS256",
    )
    user = asyncio.run(verify(token))
    assert user.id == "u1"
    assert user.email == "a@x.test"


def test_name_claims_are_carried_when_present() -> None:
    # K6-D-1 seed: Clerk custom claims given_name/family_name → the principal.
    verify = make_jwt_verifier(_cfg(secret="s3cret", algorithms=["HS256"]))
    token = jwt.encode(
        {
            "sub": "u1",
            "email": "a@x.test",
            "given_name": "Ada",
            "family_name": "Lovelace",
            "exp": int(time.time()) + 60,
        },
        "s3cret",
        algorithm="HS256",
    )
    user = asyncio.run(verify(token))
    assert user.first_name == "Ada"
    assert user.last_name == "Lovelace"


def test_absent_name_claims_default_to_none() -> None:
    # The default token (no custom claims) carries no name → the seed is a no-op.
    verify = make_jwt_verifier(_cfg(secret="s3cret", algorithms=["HS256"]))
    token = jwt.encode(
        {"sub": "u1", "email": "a@x.test", "exp": int(time.time()) + 60},
        "s3cret",
        algorithm="HS256",
    )
    user = asyncio.run(verify(token))
    assert user.first_name is None
    assert user.last_name is None


def test_hs256_fails_closed_on_garbage_token() -> None:
    verify = make_jwt_verifier(_cfg(secret="s3cret", algorithms=["HS256"]))
    with pytest.raises(AuthenticationError):
        asyncio.run(verify("not.a.jwt"))


def test_hs256_rejects_missing_sub_claim() -> None:
    verify = make_jwt_verifier(_cfg(secret="s3cret", algorithms=["HS256"]))
    token = jwt.encode(
        {"email": "a@x.test", "exp": int(time.time()) + 60},
        "s3cret",
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        asyncio.run(verify(token))


def _rsa_keypair() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pub = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return priv, pub


def test_rs256_round_trip() -> None:
    priv, pub = _rsa_keypair()
    verify = make_jwt_verifier(_cfg(public_key=pub, algorithms=["RS256"]))
    token = jwt.encode({"sub": "u1", "exp": int(time.time()) + 60}, priv, algorithm="RS256")
    assert asyncio.run(verify(token)).id == "u1"


def _forge_hs256(payload: dict[str, object], hmac_secret: str) -> str:
    """Hand-craft an HS256 JWT (an attacker would NOT use jose, which guards its
    own encode; they HMAC the signing input with the public-key bytes directly).
    """

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps(payload).encode())
    signing_input = f"{header}.{body}".encode()
    sig = hmac.new(hmac_secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{b64(sig)}"


def test_algorithm_confusion_attack_is_rejected() -> None:
    """An RS256-configured verifier MUST reject an HS256 token forged with the
    (public) RSA key as the HMAC secret. This is the spec-08 T05 security-reviewer
    HIGH finding — the verifier binds key↔alg-family so the public key can never
    be used as an HMAC secret. The persona-voice service inherits this guarantee
    by importing from persona-core (D-V1-X-jwt-verifier-extraction).
    """
    _priv, pub = _rsa_keypair()
    verify = make_jwt_verifier(_cfg(public_key=pub, algorithms=["RS256"]))
    forged = _forge_hs256({"sub": "victim", "exp": int(time.time()) + 60}, pub)
    with pytest.raises(AuthenticationError):
        asyncio.run(verify(forged))


def test_construction_fails_fast_when_alg_lacks_key() -> None:
    # RS256 configured but no public key → refuse to build (fail-fast).
    with pytest.raises(ValueError, match="PUBLIC_KEY"):
        make_jwt_verifier(_cfg(secret="s3cret", algorithms=["RS256"]))
    # HS256 configured but no secret → refuse to build.
    with pytest.raises(ValueError, match="SECRET"):
        make_jwt_verifier(_cfg(public_key="-----X-----", algorithms=["HS256"]))


def test_construction_rejects_unknown_algorithm() -> None:
    with pytest.raises(ValueError, match="unsupported JWT algorithm"):
        make_jwt_verifier(_cfg(secret="s3cret", algorithms=["none"]))


def test_construction_rejects_no_usable_alg() -> None:
    # No algorithms configured at all → no usable key pair.
    with pytest.raises(ValueError, match="no usable JWT algorithm"):
        make_jwt_verifier(_cfg(secret="s3cret", algorithms=[]))


def test_audience_enforced_when_set() -> None:
    verify = make_jwt_verifier(
        _cfg(secret="s3cret", algorithms=["HS256"], audience="persona-voice")
    )
    # Token with the wrong audience must be rejected even though the signature is valid.
    token = jwt.encode(
        {"sub": "u1", "aud": "wrong-aud", "exp": int(time.time()) + 60},
        "s3cret",
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        asyncio.run(verify(token))
    # Token with the right audience is accepted.
    token_ok = jwt.encode(
        {"sub": "u1", "aud": "persona-voice", "exp": int(time.time()) + 60},
        "s3cret",
        algorithm="HS256",
    )
    assert asyncio.run(verify(token_ok)).id == "u1"
