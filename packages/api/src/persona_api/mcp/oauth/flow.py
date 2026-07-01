"""OAuth 2.1 authorization-code + PKCE flow engine — R8 T4/T5/T6.

The provider-agnostic core of the flow: build the authorize URL, exchange a code
for tokens, and refresh with rotation. Endpoints + client credentials come from an
:class:`~persona_api.mcp.oauth.providers.OAuthProvider` (the config path, T3) or the
generic-discovery seam (T7) — this engine is the same either way.

Security floors baked in (R8-D-6): PKCE S256 on the exchange, the fixed
pre-registered redirect URI (never from the form), the ``resource`` param (RFC 8707)
when the AS is MCP-native, and generic fail-closed errors that NEVER carry a token,
code, verifier, or client secret. The token HTTP calls are the OAuth back channel —
a trusted-constant provider endpoint (HTTPS-asserted), not a user-supplied URL, so
they use a plain client (the SSRF-pinned client is for BYO MCP URLs, not this).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from persona_api.errors import MCPOAuthError

if TYPE_CHECKING:
    import httpx

    from persona_api.mcp.oauth.providers import OAuthProvider

__all__ = [
    "TokenResponse",
    "build_authorize_url",
    "callback_redirect_uri",
    "exchange_code",
    "refresh_access_token",
]

# The single fixed callback path appended to the configured HTTPS origin (R8-D-6).
# This is a WEB-app route (``mcp_oauth_redirect_base_url`` is the web origin): because
# the API authenticates with Bearer-JWT (no cookies), a provider's top-level browser
# redirect cannot carry the caller's auth to an API endpoint. The web page at this
# route extracts ``code`` + ``state`` and relays them to the AUTHENTICATED API
# callback (``POST /v1/mcp-servers/oauth/callback``), which consumes the state
# RLS-scoped to the caller. The URI is still fixed + pre-registered + config-only.
_CALLBACK_PATH = "/mcp/oauth/callback"


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """A normalized token endpoint response.

    ``refresh_token`` / ``expires_in`` are optional (not every AS issues them); a
    rotating AS (GitHub Apps, R8-D-5) returns a NEW refresh token on every refresh.
    """

    access_token: str
    refresh_token: str | None
    expires_in: int | None
    scope: str | None
    token_type: str


def callback_redirect_uri(redirect_base_url: str) -> str:
    """The single fixed, pre-registered HTTPS callback URI (R8-D-6).

    Built ONLY from operator config — never from a request/form. Fails closed if the
    base is unset (no redirect can be formed → no flow can start).
    """
    base = redirect_base_url.strip().rstrip("/")
    if not base:
        raise MCPOAuthError(
            "oauth redirect is not configured", context={"reason": "no_redirect_base"}
        )
    # OAuth 2.1: redirect URIs must be HTTPS — except localhost, which dev uses via an
    # explicit http://localhost base (allowed only there).
    is_localhost = base.startswith(("http://localhost", "http://127.0.0.1"))
    if not base.startswith("https://") and not is_localhost:
        raise MCPOAuthError("oauth redirect must be https", context={"reason": "insecure_redirect"})
    return f"{base}{_CALLBACK_PATH}"


def build_authorize_url(
    *,
    provider: OAuthProvider,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    resource: str | None = None,
) -> str:
    """Build the provider authorize URL (authorization-code + PKCE S256).

    ``resource`` (RFC 8707, audience binding) is attached only when the provider
    opts in (``sends_resource_param``) — GitHub/Google reject unknown params, so it
    is off for them and on for the MCP-native seam.
    """
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if provider.scopes:
        params["scope"] = " ".join(provider.scopes)
    if provider.sends_resource_param and resource:
        params["resource"] = resource
    return f"{provider.authorize_url}?{urlencode(params)}"


def _assert_https(url: str) -> None:
    if not url.startswith("https://"):
        raise MCPOAuthError("oauth endpoint must be https", context={"reason": "insecure_endpoint"})


def _parse_token_payload(payload: dict[str, Any]) -> TokenResponse:
    """Normalize a token endpoint JSON body → :class:`TokenResponse` (fail-closed).

    An error body (or a body with no ``access_token``) raises the generic
    :class:`MCPOAuthError` — never echoing the provider's ``error``/description,
    which can be noisy but is kept off the client surface.
    """
    access_token = payload.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise MCPOAuthError("token exchange failed", context={"reason": "no_access_token"})
    expires_in_raw = payload.get("expires_in")
    expires_in: int | None = None
    if isinstance(expires_in_raw, (int, str)):
        try:
            expires_in = int(expires_in_raw)
        except (TypeError, ValueError):
            expires_in = None
    refresh = payload.get("refresh_token")
    scope = payload.get("scope")
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh if isinstance(refresh, str) else None,
        expires_in=expires_in,
        scope=scope if isinstance(scope, str) else None,
        token_type=str(payload.get("token_type") or "bearer"),
    )


async def _post_token(
    *, http_client: httpx.AsyncClient, token_url: str, data: dict[str, str]
) -> TokenResponse:
    """POST the back-channel token request and normalize the response. Fail-closed."""
    _assert_https(token_url)
    try:
        resp = await http_client.post(
            token_url,
            data=data,
            headers={"Accept": "application/json"},
        )
    except Exception as exc:  # noqa: BLE001 — any transport error is a fail-closed oauth error
        # Type only — the exception text could echo the URL/params; keep it off the wire.
        raise MCPOAuthError(
            "token endpoint unreachable", context={"reason": "transport_error"}
        ) from exc
    if resp.status_code >= 400:
        raise MCPOAuthError("token exchange rejected", context={"reason": "token_http_error"})
    try:
        payload = resp.json()
    except ValueError as exc:
        raise MCPOAuthError(
            "token response malformed", context={"reason": "bad_token_body"}
        ) from exc
    if not isinstance(payload, dict):
        raise MCPOAuthError("token response malformed", context={"reason": "bad_token_body"})
    return _parse_token_payload(payload)


async def exchange_code(
    *,
    provider: OAuthProvider,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    http_client: httpx.AsyncClient,
    resource: str | None = None,
) -> TokenResponse:
    """Exchange an authorization ``code`` (+ PKCE verifier) for tokens (R8-D-6)."""
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider.client_id,
        "code_verifier": code_verifier,
    }
    if provider.client_secret is not None:
        data["client_secret"] = provider.client_secret.get_secret_value()
    if provider.sends_resource_param and resource:
        data["resource"] = resource
    return await _post_token(http_client=http_client, token_url=provider.token_url, data=data)


async def refresh_access_token(
    *,
    provider: OAuthProvider,
    refresh_token: str,
    http_client: httpx.AsyncClient,
    resource: str | None = None,
) -> TokenResponse:
    """Refresh with rotation (R8-D-5): a new access AND (rotating AS) new refresh token.

    The caller MUST persist the returned ``refresh_token`` atomically when present —
    a rotated (old) refresh token is one-time and its replay revokes the family.
    """
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": provider.client_id,
    }
    if provider.client_secret is not None:
        data["client_secret"] = provider.client_secret.get_secret_value()
    if provider.sends_resource_param and resource:
        data["resource"] = resource
    return await _post_token(http_client=http_client, token_url=provider.token_url, data=data)
