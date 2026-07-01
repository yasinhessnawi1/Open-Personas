"""OAuth provider registry — R8 T3 (R8-D-1 config path, R8-D-7 GitHub).

A typed, config-driven registry of the pre-registered OAuth providers Open Persona
can obtain a per-user token from. Per research §2, the concrete v1 providers
(GitHub) expose **neither** RFC 9728 PRM **nor** RFC 7591 DCR — there is nothing to
discover and no way to self-register — so the spec itself directs a hardcoded/config
``client_id``. This registry is that config path; the generic MCP-native discovery
seam (T7) is the other endpoint source for the same flow engine.

Data, not secrets: the authorize/token endpoints are trusted constants (anchored in
code, never taken from a form — mirrors N4-D-10). The ``client_id`` (public) and
``client_secret`` (secret, ``SecretStr``, never logged) come from env via
:class:`~persona_api.config.APIConfig`. A provider with no configured ``client_id``
is simply not offered (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona_api.errors import MCPOAuthProviderError

if TYPE_CHECKING:
    from pydantic import SecretStr

    from persona_api.config import APIConfig

__all__ = ["OAuthProvider", "get_provider", "provider_registry"]

# The ``mcp-native`` sentinel (T7) is reserved for the generic discovery seam; it is
# NOT a pre-registered provider and never appears in this registry.
_MCP_NATIVE_SENTINEL = "mcp-native"


@dataclass(frozen=True, slots=True)
class OAuthProvider:
    """A pre-registered OAuth authorization server + our client credentials.

    Frozen + built per-request from config; the ``client_secret`` is a ``SecretStr``
    so it never renders in a repr/log. ``sends_resource_param`` (RFC 8707) is False
    for GitHub/Google (they ignore it and some reject unknown params); the MCP-native
    seam sets it True. Endpoints are HTTPS constants (OAuth 2.1 requires HTTPS).
    """

    key: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]
    client_id: str
    client_secret: SecretStr | None
    sends_resource_param: bool = False


def _github(config: APIConfig) -> OAuthProvider | None:
    """Build the GitHub provider from config, or None when not configured."""
    client_id = config.mcp_oauth_github_client_id.strip()
    if not client_id:
        return None
    scopes = tuple(s for s in config.mcp_oauth_github_scopes.split() if s)
    return OAuthProvider(
        key="github",
        # Trusted constants (GitHub OAuth endpoints). NEVER sourced from a form.
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",
        scopes=scopes,
        client_id=client_id,
        client_secret=config.mcp_oauth_github_client_secret,
    )


def provider_registry(config: APIConfig) -> dict[str, OAuthProvider]:
    """The configured pre-registered providers, keyed by provider name.

    Empty when nothing is configured (GitHub off) — the config path offers no
    provider until the operator completes the T9.5 app-registration runbook.
    """
    registry: dict[str, OAuthProvider] = {}
    github = _github(config)
    if github is not None:
        registry[github.key] = github
    return registry


def get_provider(config: APIConfig, key: str) -> OAuthProvider:
    """Return the configured provider for ``key``, or fail closed.

    Raises:
        MCPOAuthProviderError: ``key`` is unknown / not configured (or the reserved
            ``mcp-native`` sentinel, which is the T7 seam's, not a registry entry).
    """
    provider = provider_registry(config).get(key)
    if provider is None:
        raise MCPOAuthProviderError(
            "oauth provider is not available", context={"reason": "provider_not_configured"}
        )
    return provider
