"""Generic MCP-native OAuth discovery + DCR — R8 T7 (R8-D-1/2/3).

The spec-mandated client-side discovery chain (research §1) for a remote MCP server
that actually advertises OAuth metadata — the seam the config path (GitHub) cannot
use because GitHub exposes neither PRM nor DCR:

1. probe the MCP URL unauthenticated → **401 + ``WWW-Authenticate``** carrying the
   ``resource_metadata`` URL (RFC 9728 §5.1);
2. ``GET`` that → **Protected Resource Metadata** (RFC 9728) → ``authorization_servers``;
3. ``GET`` the AS's ``/.well-known/oauth-authorization-server`` → **AS metadata**
   (RFC 8414): authorize / token / registration endpoints;
4. **DCR** (RFC 7591): ``POST`` the registration endpoint with our SINGLE fixed callback
   as the only ``redirect_uri`` → a ``client_id`` (public client, no secret).

Security: every discovered URL is run through the N1/N4 **SSRF gate**
(:func:`assert_url_allowed`) before we fetch it — a malicious MCP server must not be
able to point discovery at an internal/metadata address. All endpoints must be HTTPS.
DCR registers ONLY our fixed callback (research §3.4: unrestricted DCR redirect_uri =
code exfiltration). Any failure is fail-closed → :class:`MCPOAuthProviderError`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from persona.errors import MCPUrlNotAllowedError
from persona.tools.mcp.ssrf import assert_url_allowed

from persona_api.errors import MCPOAuthProviderError

if TYPE_CHECKING:
    import httpx

__all__ = ["DiscoveredAS", "discover_authorization_server", "register_client"]

# ``WWW-Authenticate: Bearer …, resource_metadata="https://…"`` (RFC 9728 §5.1).
_RESOURCE_METADATA_RE = re.compile(r'resource_metadata\s*=\s*"([^"]+)"')
_WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"


@dataclass(frozen=True, slots=True)
class DiscoveredAS:
    """The endpoints discovered for an MCP-native authorization server."""

    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None


def _safe_https(url: str) -> str:
    """SSRF-gate + https-check a discovered URL before we fetch it. Fail-closed."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise MCPOAuthProviderError(
            "discovered oauth endpoint is not https", context={"reason": "insecure_endpoint"}
        )
    try:
        assert_url_allowed(url)
    except MCPUrlNotAllowedError as exc:
        # A discovered endpoint pointing at a private/loopback/metadata target — refuse.
        raise MCPOAuthProviderError(
            "discovered oauth endpoint is not allowed", context={"reason": "ssrf_blocked"}
        ) from exc
    return url


async def _get_json(http_client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    resp = await http_client.get(_safe_https(url))
    if resp.status_code >= 400:
        raise MCPOAuthProviderError(
            "oauth discovery request failed", context={"reason": "discovery_http_error"}
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise MCPOAuthProviderError(
            "oauth discovery response malformed", context={"reason": "discovery_bad_body"}
        ) from exc
    if not isinstance(payload, dict):
        raise MCPOAuthProviderError(
            "oauth discovery response malformed", context={"reason": "discovery_bad_body"}
        )
    return payload


async def discover_authorization_server(
    *, http_client: httpx.AsyncClient, mcp_url: str
) -> DiscoveredAS:
    """Run the 401 → PRM → AS-metadata chain for ``mcp_url``. Fail-closed.

    Raises:
        MCPOAuthProviderError: the server is not OAuth-protected / does not advertise
            the required metadata, or a discovered URL is unsafe.
    """
    # 1. Probe unauthenticated → expect 401 + WWW-Authenticate(resource_metadata=…).
    _safe_https(mcp_url)
    probe = await http_client.get(mcp_url)
    if probe.status_code != 401:
        raise MCPOAuthProviderError(
            "mcp server is not oauth-protected", context={"reason": "not_protected"}
        )
    match = _RESOURCE_METADATA_RE.search(probe.headers.get("WWW-Authenticate", ""))
    if not match:
        raise MCPOAuthProviderError(
            "mcp server did not advertise resource metadata",
            context={"reason": "no_resource_metadata"},
        )
    # 2. Protected Resource Metadata → authorization_servers.
    prm = await _get_json(http_client, match.group(1))
    servers = prm.get("authorization_servers")
    if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
        raise MCPOAuthProviderError(
            "no authorization server advertised", context={"reason": "no_as"}
        )
    # 3. AS metadata (RFC 8414) at the issuer's well-known path.
    as_meta_url = servers[0].rstrip("/") + _WELL_KNOWN_AS
    meta = await _get_json(http_client, as_meta_url)
    authorize = meta.get("authorization_endpoint")
    token = meta.get("token_endpoint")
    if not isinstance(authorize, str) or not isinstance(token, str):
        raise MCPOAuthProviderError(
            "authorization server metadata incomplete", context={"reason": "as_meta_incomplete"}
        )
    registration = meta.get("registration_endpoint")
    return DiscoveredAS(
        authorization_endpoint=_safe_https(authorize),
        token_endpoint=_safe_https(token),
        registration_endpoint=(
            _safe_https(registration) if isinstance(registration, str) else None
        ),
    )


async def register_client(
    *, http_client: httpx.AsyncClient, registration_endpoint: str, redirect_uri: str
) -> str:
    """RFC 7591 DCR: register a public client with our fixed callback → ``client_id``.

    Registers ONLY ``redirect_uri`` (our single pre-registered callback) so a
    discovered AS cannot be induced to accept an exfiltration redirect (research §3.4).
    Public client (``token_endpoint_auth_method = none``): PKCE-only, no client secret.
    """
    body = {
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": "Open Persona",
    }
    try:
        resp = await http_client.post(_safe_https(registration_endpoint), json=body)
    except Exception as exc:  # noqa: BLE001 — any transport error is fail-closed
        raise MCPOAuthProviderError(
            "dynamic client registration failed", context={"reason": "dcr_transport"}
        ) from exc
    if resp.status_code >= 400:
        raise MCPOAuthProviderError(
            "dynamic client registration rejected", context={"reason": "dcr_http_error"}
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise MCPOAuthProviderError(
            "dynamic client registration malformed", context={"reason": "dcr_bad_body"}
        ) from exc
    client_id = payload.get("client_id") if isinstance(payload, dict) else None
    if not client_id or not isinstance(client_id, str):
        raise MCPOAuthProviderError(
            "dynamic client registration returned no client_id",
            context={"reason": "dcr_no_client_id"},
        )
    return str(client_id)
