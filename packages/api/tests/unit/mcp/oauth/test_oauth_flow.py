"""R8 T4/T5/T6 — flow-engine contract: authorize URL, redirect URI, exchange, refresh."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from persona_api.errors import MCPOAuthError
from persona_api.mcp.oauth.flow import (
    build_authorize_url,
    callback_redirect_uri,
    exchange_code,
    refresh_access_token,
)
from persona_api.mcp.oauth.providers import OAuthProvider
from pydantic import SecretStr

_GH = OAuthProvider(
    key="github",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    scopes=("repo",),
    client_id="cid_123",
    client_secret=SecretStr("csecret_xyz"),
)
_MCP_NATIVE = OAuthProvider(
    key="mcp-native",
    authorize_url="https://as.example/authorize",
    token_url="https://as.example/token",
    scopes=(),
    client_id="dcr_cid",
    client_secret=None,
    sends_resource_param=True,
)


class TestCallbackRedirectUri:
    def test_builds_fixed_callback(self) -> None:
        assert (
            callback_redirect_uri("https://api.openpersona.ai")
            == "https://api.openpersona.ai/mcp/oauth/callback"
        )

    def test_trailing_slash_normalized(self) -> None:
        assert (
            callback_redirect_uri("https://api.openpersona.ai/")
            == "https://api.openpersona.ai/mcp/oauth/callback"
        )

    def test_empty_base_fails_closed(self) -> None:
        with pytest.raises(MCPOAuthError):
            callback_redirect_uri("")

    def test_non_https_non_localhost_rejected(self) -> None:
        with pytest.raises(MCPOAuthError):
            callback_redirect_uri("http://evil.example")

    def test_localhost_http_allowed_for_dev(self) -> None:
        assert callback_redirect_uri("http://localhost:8000").startswith("http://localhost:8000/")


class TestBuildAuthorizeUrl:
    def _params(self, url: str) -> dict[str, list[str]]:
        return parse_qs(urlparse(url).query)

    def test_has_pkce_and_state_and_redirect(self) -> None:
        url = build_authorize_url(
            provider=_GH,
            redirect_uri="https://api.openpersona.ai/mcp/oauth/callback",
            state="STATE_OPAQUE",
            code_challenge="CHALLENGE_ABC",
        )
        assert url.startswith("https://github.com/login/oauth/authorize?")
        p = self._params(url)
        assert p["response_type"] == ["code"]
        assert p["client_id"] == ["cid_123"]
        assert p["redirect_uri"] == ["https://api.openpersona.ai/mcp/oauth/callback"]
        assert p["state"] == ["STATE_OPAQUE"]
        assert p["code_challenge"] == ["CHALLENGE_ABC"]
        assert p["code_challenge_method"] == ["S256"]
        assert p["scope"] == ["repo"]

    def test_github_omits_resource_param(self) -> None:
        url = build_authorize_url(
            provider=_GH,
            redirect_uri="https://x/cb",
            state="s",
            code_challenge="c",
            resource="https://api.github.com/mcp",
        )
        assert "resource=" not in url

    def test_mcp_native_includes_resource_param(self) -> None:
        url = build_authorize_url(
            provider=_MCP_NATIVE,
            redirect_uri="https://x/cb",
            state="s",
            code_challenge="c",
            resource="https://as.example/mcp",
        )
        assert self._params(url)["resource"] == ["https://as.example/mcp"]

    def test_no_secret_in_authorize_url(self) -> None:
        url = build_authorize_url(
            provider=_GH, redirect_uri="https://x/cb", state="s", code_challenge="c"
        )
        assert "csecret_xyz" not in url  # client_secret NEVER on the front channel


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


class TestExchangeCode:
    @pytest.mark.asyncio
    async def test_happy_path_parses_tokens_and_sends_pkce(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(parse_qs(request.content.decode()))
            assert request.headers["Accept"] == "application/json"
            return httpx.Response(
                200,
                json={
                    "access_token": "at_new",
                    "refresh_token": "rt_new",
                    "expires_in": 28800,
                    "scope": "repo",
                    "token_type": "bearer",
                },
            )

        async with _client(handler) as client:
            tok = await exchange_code(
                provider=_GH,
                redirect_uri="https://x/cb",
                code="CODE_123",
                code_verifier="VERIFIER_ABC",
                http_client=client,
            )
        assert tok.access_token == "at_new"
        assert tok.refresh_token == "rt_new"
        assert tok.expires_in == 28800
        # PKCE verifier + client_secret + grant on the back channel.
        assert seen["grant_type"] == ["authorization_code"]
        assert seen["code_verifier"] == ["VERIFIER_ABC"]
        assert seen["code"] == ["CODE_123"]
        assert seen["client_secret"] == ["csecret_xyz"]

    @pytest.mark.asyncio
    async def test_http_error_is_failclosed(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "bad_verification_code"})

        async with _client(handler) as client:
            with pytest.raises(MCPOAuthError):
                await exchange_code(
                    provider=_GH,
                    redirect_uri="https://x/cb",
                    code="bad",
                    code_verifier="v",
                    http_client=client,
                )

    @pytest.mark.asyncio
    async def test_missing_access_token_is_failclosed(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"token_type": "bearer"})  # no access_token

        async with _client(handler) as client:
            with pytest.raises(MCPOAuthError):
                await exchange_code(
                    provider=_GH,
                    redirect_uri="https://x/cb",
                    code="c",
                    code_verifier="v",
                    http_client=client,
                )


class TestRefreshRotation:
    @pytest.mark.asyncio
    async def test_refresh_returns_rotated_tokens(self) -> None:
        seen: dict[str, list[str]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(parse_qs(request.content.decode()))
            return httpx.Response(
                200,
                json={
                    "access_token": "at_rotated",
                    "refresh_token": "rt_rotated",  # NEW refresh token (rotation)
                    "expires_in": 28800,
                    "token_type": "bearer",
                },
            )

        async with _client(handler) as client:
            tok = await refresh_access_token(
                provider=_GH, refresh_token="rt_old", http_client=client
            )
        assert tok.access_token == "at_rotated"
        assert tok.refresh_token == "rt_rotated"
        assert seen["grant_type"] == ["refresh_token"]
        assert seen["refresh_token"] == ["rt_old"]
