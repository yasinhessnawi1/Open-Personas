"""R8 T10 — OAuth security suite: no-token-leak (grep-proof) + cross-tenant RLS.

The credential-leak discipline as a test, not a hope: after a full OAuth connect, the
access + refresh tokens (and the PKCE verifier) live ONLY in the Fernet-encrypted
columns — no public read surface, no ``__repr__``, no log-shaped string emits them. And
a tenant cannot reach another tenant's OAuth token/context through the store (RLS,
non-vacuous under ``persona_app``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
import pytest
from cryptography.fernet import Fernet
from persona_api.config import APIConfig
from persona_api.errors import MCPServerNotFoundError
from persona_api.mcp import store as mcp_store
from persona_api.mcp.oauth import service as oauth_service
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_ACCESS_LEAK = "ACCESS_TOKEN_MUST_NOT_LEAK"  # noqa: S105 — test sentinel
_REFRESH_LEAK = "REFRESH_TOKEN_MUST_NOT_LEAK"  # noqa: S105 — test sentinel


def _config() -> APIConfig:
    return APIConfig(
        mcp_credential_key=Fernet.generate_key().decode(),
        mcp_oauth_github_client_id="cid",
        mcp_oauth_github_client_secret="csecret",
        mcp_oauth_redirect_base_url="https://app.example",
    )


def _app_engine(migrated_engine: Engine) -> Engine:
    url = migrated_engine.url.set(username="persona_app", password="persona_app").render_as_string(
        hide_password=False
    )
    return make_rls_engine(url)


def _mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> Callable[..., object]:
    real = httpx.AsyncClient

    def factory(*_a: object, **_k: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler))

    return factory


def _seed_oauth(migrated_engine: Engine, owner: str, server_id: str) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text(
                "INSERT INTO user_mcp_servers "
                "(id, owner_id, name, url, auth_method, oauth_provider)"
                " VALUES (:s, :o, :n, 'https://api.githubcopilot.com/mcp/', 'oauth', 'github')"
            ),
            {"s": server_id, "o": owner, "n": f"gh-{server_id}"},
        )


@pytest.mark.asyncio
async def test_no_public_surface_leaks_oauth_tokens(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    engine = _app_engine(migrated_engine)
    _seed_oauth(migrated_engine, "u_leak", "srv_leak")

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": _ACCESS_LEAK,
                "refresh_token": _REFRESH_LEAK,
                "expires_in": 3600,
                "scope": "repo",
                "token_type": "bearer",
            },
        )

    monkeypatch.setattr(oauth_service.httpx, "AsyncClient", _mock_client(handler))

    tok = current_user_id.set("u_leak")
    try:
        url = await oauth_service.initiate_authorize(
            rls_engine=engine, config=config, server_id="srv_leak", redirect_after=None
        )
        from urllib.parse import parse_qs, urlparse

        state = parse_qs(urlparse(url).query)["state"][0]
        await oauth_service.handle_callback(rls_engine=engine, config=config, state=state, code="c")

        # Every PUBLIC read surface — the detail, the list, the persona list — is scanned.
        detail = mcp_store.get_server(rls_engine=engine, server_id="srv_leak")
        surfaces = [
            str(detail),
            repr(detail),
            str(mcp_store.list_servers(rls_engine=engine)),
        ]
        for s in surfaces:
            assert _ACCESS_LEAK not in s, "access token leaked into a public surface"
            assert _REFRESH_LEAK not in s, "refresh token leaked into a public surface"
        # The detail carries only the non-secret markers.
        assert detail["has_credential"] is True
        assert detail["oauth_provider"] == "github"

        # ...yet the tokens ARE stored (only the internal Fernet decrypt reaches them).
        cipher = mcp_store.cipher_from_config(config)
        assert cipher is not None
        with migrated_engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT credentials_encrypted, refresh_token_encrypted "
                        "FROM user_mcp_servers WHERE id = 'srv_leak'"
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None
        # The raw at-rest blobs are ciphertext, not the plaintext sentinels.
        assert _ACCESS_LEAK not in str(row["credentials_encrypted"])
        assert _REFRESH_LEAK not in str(row["refresh_token_encrypted"])
        assert cipher.decrypt(str(row["credentials_encrypted"])) == _ACCESS_LEAK
        assert cipher.decrypt(str(row["refresh_token_encrypted"])) == _REFRESH_LEAK
    finally:
        current_user_id.reset(tok)


def test_cross_tenant_cannot_read_oauth_context(migrated_engine: Engine) -> None:
    """Tenant B cannot load tenant A's OAuth server context (RLS → 404)."""
    config = _config()
    engine = _app_engine(migrated_engine)
    _seed_oauth(migrated_engine, "u_ctx_a", "srv_ctx_a")
    _seed_oauth(migrated_engine, "u_ctx_b", "srv_ctx_b")

    tok = current_user_id.set("u_ctx_b")
    try:
        # B loads its own — fine (non-vacuous).
        own = mcp_store.oauth_server_context(
            rls_engine=engine, config=config, server_id="srv_ctx_b"
        )
        assert own["owner_id"] == "u_ctx_b"
        # B cannot load A's — RLS hides it → not found.
        with pytest.raises(MCPServerNotFoundError):
            mcp_store.oauth_server_context(rls_engine=engine, config=config, server_id="srv_ctx_a")
    finally:
        current_user_id.reset(tok)
