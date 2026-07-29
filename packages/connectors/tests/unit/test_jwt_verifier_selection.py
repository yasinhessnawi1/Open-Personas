"""Tests for the connector's JWT-verifier selection (R9-062).

The connector service verified Clerk tokens ONLY via the static-key
``persona.auth.jwt_verifier.make_jwt_verifier`` — pinned to one Clerk
instance's public key, breaking on any other instance (e.g. dev vs prod) or on
key rotation. ``_build_connector_verifier`` (``persona_connectors.__main__``)
is the ONE selection point every ``_setup_*`` call site now goes through:
``PERSONA_CONNECTORS_JWT_JWKS_URL`` set → the new JWKS-based verifier
(``make_jwks_verifier``); unset (the default) → the existing static verifier,
byte-identical to every deployment predating this option.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from jose.backends.cryptography_backend import CryptographyRSAKey
from persona.errors import AuthenticationError
from persona_connectors.__main__ import _build_connector_verifier
from persona_connectors.config import ConnectorConfig


def _rsa_keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


def _jwk_from_public_key(public_key: rsa.RSAPublicKey, kid: str) -> dict[str, str]:
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


def test_static_verifier_selected_when_jwks_url_unset() -> None:
    """No ``jwt_jwks_url`` → the existing static verifier, byte-identical behavior."""
    config = ConnectorConfig(jwt_secret="s3cret", jwt_algorithms="HS256")  # type: ignore[call-arg]
    verify = _build_connector_verifier(config, httpx.AsyncClient())
    token = jwt.encode({"sub": "u1", "exp": int(time.time()) + 60}, "s3cret", algorithm="HS256")
    user = asyncio.run(verify(token))
    assert user.id == "u1"


def test_jwks_verifier_selected_when_jwks_url_set() -> None:
    """``jwt_jwks_url`` set → the JWKS verifier, fetching over the shared httpx client."""
    priv, pub = _rsa_keypair()
    jwk = _jwk_from_public_key(pub, kid="key-1")
    token = _sign(priv, "key-1", {"sub": "u1", "exp": int(time.time()) + 60})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://issuer.test/.well-known/jwks.json"
        return httpx.Response(200, json={"keys": [jwk]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ConnectorConfig(
        jwt_algorithms="RS256",
        jwt_jwks_url="https://issuer.test/.well-known/jwks.json",
    )  # type: ignore[call-arg]
    verify = _build_connector_verifier(config, http)
    user = asyncio.run(verify(token))
    assert user.id == "u1"


def test_jwks_verifier_fails_closed_on_unknown_kid() -> None:
    priv, _pub = _rsa_keypair()
    token = _sign(priv, "missing-key", {"sub": "u1", "exp": int(time.time()) + 60})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": []})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    config = ConnectorConfig(
        jwt_algorithms="RS256",
        jwt_jwks_url="https://issuer.test/.well-known/jwks.json",
    )  # type: ignore[call-arg]
    verify = _build_connector_verifier(config, http)
    with pytest.raises(AuthenticationError):
        asyncio.run(verify(token))
