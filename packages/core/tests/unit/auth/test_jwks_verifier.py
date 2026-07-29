"""Unit tests for the JWKS-based JWT verifier (R9-062).

The connector service verifies Clerk JWTs via the static-key
``make_jwt_verifier`` (pinned to one Clerk instance's signing key). That pins
the verifier to one instance and breaks on key rotation. ``make_jwks_verifier``
is an ADDITIVE builder — same :class:`~persona.auth.AuthenticatedUser` contract
— that resolves the verification key per-token from a JWKS document (matched
by ``kid``), so one connector process can verify tokens from any instance of a
provider and survive rotation. No network access here: ``fetch_jwks`` is
injected as a fake.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.backends.cryptography_backend import CryptographyRSAKey
from persona.auth import AuthenticatedUser, make_jwks_verifier
from persona.errors import AuthenticationError


def _rsa_keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _jwk_from_public_key(public_key: rsa.RSAPublicKey, kid: str) -> dict[str, str]:
    """Build a JWKS-shaped JWK dict (RSA, ``kid``-tagged) from a public key.

    python-jose's ``CryptographyRSAKey`` wraps an RSA public key, and its
    ``to_dict()`` emits the standard JWK fields (``kty``, ``n``, ``e``) —
    exactly what a real JWKS endpoint publishes.
    """
    rsa_key = CryptographyRSAKey(public_key, algorithm="RS256")
    jwk_dict = rsa_key.to_dict()
    jwk_dict["kid"] = kid
    return jwk_dict


def _sign(private_key: rsa.RSAPrivateKey, kid: str, claims: dict[str, object]) -> str:
    priv_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


def _jwks_of(*jwks: dict[str, str]) -> dict[str, object]:
    return {"keys": list(jwks)}


async def _never() -> dict[str, object]:
    raise AssertionError("fetch_jwks should not have been called")


def test_valid_token_matching_kid_and_audience_resolves_user() -> None:
    priv, pub = _rsa_keypair()
    jwk = _jwk_from_public_key(pub, kid="key-1")
    token = _sign(
        priv,
        "key-1",
        {"sub": "u1", "aud": "connectors", "exp": int(time.time()) + 60},
    )

    async def fetch_jwks() -> dict[str, object]:
        return _jwks_of(jwk)

    verify = make_jwks_verifier(
        jwks_url="https://issuer.test/.well-known/jwks.json",
        audience="connectors",
        algorithms=["RS256"],
        fetch_jwks=fetch_jwks,
    )
    user = asyncio.run(verify(token))
    assert user == AuthenticatedUser(id="u1")


def test_unknown_kid_refetches_once_then_fails_closed() -> None:
    priv, _pub = _rsa_keypair()
    token = _sign(priv, "missing-key", {"sub": "u1", "exp": int(time.time()) + 60})
    calls = 0

    async def fetch_jwks() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _jwks_of()  # never has the kid the token claims

    verify = make_jwks_verifier(
        jwks_url="https://issuer.test/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        fetch_jwks=fetch_jwks,
    )
    with pytest.raises(AuthenticationError, match="unknown signing key"):
        asyncio.run(verify(token))
    # Initial fetch (cache miss) + exactly ONE refetch on the unresolved kid.
    assert calls == 2


def test_kid_present_but_signed_by_a_different_key_is_rejected() -> None:
    priv_real, _pub_real = _rsa_keypair()
    _priv_other, pub_other = _rsa_keypair()
    # The JWKS advertises "key-1" bound to a DIFFERENT public key than the one
    # that actually signed the token — the classic signature-mismatch case.
    jwk = _jwk_from_public_key(pub_other, kid="key-1")
    token = _sign(priv_real, "key-1", {"sub": "u1", "exp": int(time.time()) + 60})

    async def fetch_jwks() -> dict[str, object]:
        return _jwks_of(jwk)

    verify = make_jwks_verifier(
        jwks_url="https://issuer.test/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        fetch_jwks=fetch_jwks,
    )
    with pytest.raises(AuthenticationError):
        asyncio.run(verify(token))


def test_hs256_algorithm_confusion_is_rejected_even_with_a_symmetric_secret() -> None:
    # A JWKS verifier has NO symmetric secret to be confused with — but a
    # forged/self-issued HS256 token must still never be accepted, regardless
    # of any secret an attacker might guess or hold.
    forged = jwt.encode(
        {"sub": "victim", "exp": int(time.time()) + 60}, "some-secret", algorithm="HS256"
    )

    verify = make_jwks_verifier(
        jwks_url="https://issuer.test/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        fetch_jwks=lambda: _never(),
    )
    with pytest.raises(AuthenticationError, match="algorithm not allowed"):
        asyncio.run(verify(forged))


def test_wrong_audience_is_rejected() -> None:
    priv, pub = _rsa_keypair()
    jwk = _jwk_from_public_key(pub, kid="key-1")
    token = _sign(priv, "key-1", {"sub": "u1", "aud": "other-app", "exp": int(time.time()) + 60})

    async def fetch_jwks() -> dict[str, object]:
        return _jwks_of(jwk)

    verify = make_jwks_verifier(
        jwks_url="https://issuer.test/.well-known/jwks.json",
        audience="connectors",
        algorithms=["RS256"],
        fetch_jwks=fetch_jwks,
    )
    with pytest.raises(AuthenticationError):
        asyncio.run(verify(token))


def test_missing_sub_claim_is_rejected() -> None:
    priv, pub = _rsa_keypair()
    jwk = _jwk_from_public_key(pub, kid="key-1")
    token = _sign(priv, "key-1", {"exp": int(time.time()) + 60})

    async def fetch_jwks() -> dict[str, object]:
        return _jwks_of(jwk)

    verify = make_jwks_verifier(
        jwks_url="https://issuer.test/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        fetch_jwks=fetch_jwks,
    )
    with pytest.raises(AuthenticationError):
        asyncio.run(verify(token))


def test_expired_token_is_rejected() -> None:
    priv, pub = _rsa_keypair()
    jwk = _jwk_from_public_key(pub, kid="key-1")
    token = _sign(priv, "key-1", {"sub": "u1", "exp": int(time.time()) - 60})

    async def fetch_jwks() -> dict[str, object]:
        return _jwks_of(jwk)

    verify = make_jwks_verifier(
        jwks_url="https://issuer.test/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        fetch_jwks=fetch_jwks,
    )
    with pytest.raises(AuthenticationError):
        asyncio.run(verify(token))


def test_fetch_failure_with_no_cache_fails_closed() -> None:
    priv, _pub = _rsa_keypair()
    token = _sign(priv, "key-1", {"sub": "u1", "exp": int(time.time()) + 60})

    async def fetch_jwks() -> dict[str, object]:
        raise RuntimeError("network is down")

    verify = make_jwks_verifier(
        jwks_url="https://issuer.test/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        fetch_jwks=fetch_jwks,
    )
    with pytest.raises(AuthenticationError, match="failed to fetch JWKS"):
        asyncio.run(verify(token))


def test_construction_rejects_symmetric_algorithms() -> None:
    with pytest.raises(ValueError, match="asymmetric"):
        make_jwks_verifier(
            jwks_url="https://issuer.test/.well-known/jwks.json",
            audience=None,
            algorithms=["HS256"],
            fetch_jwks=lambda: _never(),
        )


def test_construction_rejects_no_algorithms() -> None:
    with pytest.raises(ValueError, match="no usable asymmetric"):
        make_jwks_verifier(
            jwks_url="https://issuer.test/.well-known/jwks.json",
            audience=None,
            algorithms=[],
            fetch_jwks=lambda: _never(),
        )


def test_cache_is_reused_within_ttl_no_refetch_for_a_known_kid() -> None:
    priv, pub = _rsa_keypair()
    jwk = _jwk_from_public_key(pub, kid="key-1")
    token = _sign(priv, "key-1", {"sub": "u1", "exp": int(time.time()) + 60})
    calls = 0

    async def fetch_jwks() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _jwks_of(jwk)

    verify = make_jwks_verifier(
        jwks_url="https://issuer.test/.well-known/jwks.json",
        audience=None,
        algorithms=["RS256"],
        fetch_jwks=fetch_jwks,
        cache_ttl_seconds=600,
    )
    asyncio.run(verify(token))
    asyncio.run(verify(token))
    assert calls == 1  # the second verify reused the cached JWKS
