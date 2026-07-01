"""R8 T3 — OAuth provider registry contract (config path, GitHub pre-registered)."""

from __future__ import annotations

import pytest
from persona_api.config import APIConfig
from persona_api.errors import MCPOAuthProviderError
from persona_api.mcp.oauth.providers import get_provider, provider_registry


def _config(**kw: object) -> APIConfig:
    base: dict[str, object] = {
        "database_url": "postgresql+psycopg://super@localhost/persona_shell",
        "app_database_url": "postgresql+psycopg://persona_app@localhost/persona_shell",
    }
    base.update(kw)
    return APIConfig(**base)  # type: ignore[arg-type]


class TestRegistry:
    def test_github_absent_when_no_client_id(self) -> None:
        # Fail-closed: no client_id configured → GitHub is not offered.
        assert provider_registry(_config()) == {}

    def test_github_present_when_configured(self) -> None:
        reg = provider_registry(
            _config(
                mcp_oauth_github_client_id="gh_client_123",
                mcp_oauth_github_client_secret="gh_secret_xyz",
            )
        )
        assert "github" in reg
        gh = reg["github"]
        assert gh.authorize_url == "https://github.com/login/oauth/authorize"
        assert gh.token_url == "https://github.com/login/oauth/access_token"
        assert gh.client_id == "gh_client_123"
        assert gh.scopes == ("repo",)  # least-privilege default

    def test_endpoints_are_https(self) -> None:
        gh = provider_registry(_config(mcp_oauth_github_client_id="x"))["github"]
        assert gh.authorize_url.startswith("https://")
        assert gh.token_url.startswith("https://")

    def test_custom_scopes_parsed_space_delimited(self) -> None:
        gh = provider_registry(
            _config(mcp_oauth_github_client_id="x", mcp_oauth_github_scopes="repo read:org")
        )["github"]
        assert gh.scopes == ("repo", "read:org")


class TestSecretHandling:
    def test_client_secret_not_in_provider_repr(self) -> None:
        gh = provider_registry(
            _config(
                mcp_oauth_github_client_id="x",
                mcp_oauth_github_client_secret="TOP_SECRET_VALUE",
            )
        )["github"]
        # SecretStr → the plaintext never renders in a repr/log line.
        assert "TOP_SECRET_VALUE" not in repr(gh)
        assert gh.client_secret is not None
        assert gh.client_secret.get_secret_value() == "TOP_SECRET_VALUE"


class TestGetProvider:
    def test_unknown_provider_fails_closed(self) -> None:
        with pytest.raises(MCPOAuthProviderError):
            get_provider(_config(mcp_oauth_github_client_id="x"), "notaprovider")

    def test_mcp_native_sentinel_is_not_a_registry_provider(self) -> None:
        # The generic-seam sentinel is never a pre-registered provider.
        with pytest.raises(MCPOAuthProviderError):
            get_provider(_config(mcp_oauth_github_client_id="x"), "mcp-native")

    def test_returns_configured_github(self) -> None:
        gh = get_provider(_config(mcp_oauth_github_client_id="x"), "github")
        assert gh.key == "github"
