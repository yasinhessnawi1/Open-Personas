"""OAuth flow orchestration — R8 T4/T5/T6 (ties store + state + providers + flow).

Three entry points, all fail-closed:

- :func:`initiate_authorize` (T4) — mint state + PKCE, return the provider authorize
  URL. Sync (no network). RLS-scoped: the server must be the caller's.
- :func:`handle_callback` (T5) — consume state (CSRF/owner/expiry), exchange the code
  for tokens on the back channel, persist them. Async. Any failure ⇒ no token written.
- :func:`refresh_persona_oauth_servers` (T6) — before a persona's toolbox is built,
  refresh any near-expiry access token (rotation, persist newest). A hard failure
  drops that server to not-connected (never a silent stale/partial state).

The ``mcp-native`` provider (T7) is resolved through :func:`_resolve_provider`, the
single seam the generic-discovery path plugs into; today it raises for that sentinel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from persona.logging import get_logger

from persona_api.errors import MCPOAuthError, MCPOAuthProviderError
from persona_api.mcp import store as mcp_store
from persona_api.mcp.oauth import state_store
from persona_api.mcp.oauth.discovery import discover_authorization_server, register_client
from persona_api.mcp.oauth.flow import (
    build_authorize_url,
    callback_redirect_uri,
    exchange_code,
    refresh_access_token,
)
from persona_api.mcp.oauth.providers import OAuthProvider, get_provider

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from persona_api.config import APIConfig
    from persona_api.mcp.oauth.flow import TokenResponse

__all__ = [
    "CallbackResult",
    "handle_callback",
    "initiate_authorize",
    "reauthenticate_server",
    "refresh_persona_oauth_servers",
]


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """The outcome of a completed OAuth callback: which server connected + where to go."""

    server_id: str
    redirect_after: str | None


_log = get_logger("api.mcp.oauth")

# Back-channel token HTTP timeout (connect + read). Short — a slow provider token
# endpoint must not hang a persona load; a timeout is a fail-closed oauth error.
_HTTP_TIMEOUT = httpx.Timeout(10.0)

_MCP_NATIVE = "mcp-native"


async def _resolve_provider(
    *, rls_engine: Engine, config: APIConfig, ctx: dict[str, object]
) -> OAuthProvider:
    """Resolve a server's OAuth provider — config path (T3) or generic seam (T7).

    Config path (e.g. ``github``): endpoints + client creds from the registry, no
    network. Generic ``mcp-native`` path: run 401→PRM→AS-metadata discovery against the
    server URL, DCR-register (once, persisting the client_id) if none is stored yet, and
    build a public-client provider with ``sends_resource_param`` on (RFC 8707). Endpoints
    are RE-discovered each call; only the DCR client_id is persisted (it can't be
    re-derived). Any discovery/DCR failure is fail-closed (:class:`MCPOAuthProviderError`).
    """
    provider_key = str(ctx["oauth_provider"])
    if provider_key != _MCP_NATIVE:
        return get_provider(config, provider_key)
    server_url = str(ctx["url"])
    redirect_uri = callback_redirect_uri(config.mcp_oauth_redirect_base_url)
    # follow_redirects off: the authorize redirect is the USER's browser hop, never a
    # server-side follow; discovery + DCR are direct fetches to gated https endpoints.
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, follow_redirects=False) as client:
        discovered = await discover_authorization_server(http_client=client, mcp_url=server_url)
        client_id = ctx.get("oauth_client_id")
        if not client_id:
            if not discovered.registration_endpoint:
                raise MCPOAuthProviderError(
                    "discovered authorization server offers no DCR endpoint",
                    context={"reason": "no_dcr"},
                )
            client_id = await register_client(
                http_client=client,
                registration_endpoint=discovered.registration_endpoint,
                redirect_uri=redirect_uri,
            )
            mcp_store.store_oauth_client_id(
                rls_engine=rls_engine, server_id=str(ctx["id"]), client_id=client_id
            )
    return OAuthProvider(
        key=_MCP_NATIVE,
        authorize_url=discovered.authorization_endpoint,
        token_url=discovered.token_endpoint,
        scopes=(),
        client_id=str(client_id),
        client_secret=None,
        sends_resource_param=True,
    )


async def initiate_authorize(
    *,
    rls_engine: Engine,
    config: APIConfig,
    server_id: str,
    redirect_after: str | None,
) -> str:
    """Start an OAuth flow for the caller's server; return the provider authorize URL.

    Async because the generic ``mcp-native`` path discovers + DCR-registers here (the
    config path does no network). Raises:
        MCPServerNotFoundError: the server is not the caller's (RLS → 404).
        MCPOAuthProviderError: the server has no oauth provider / it is unconfigured /
            discovery or DCR failed.
        MCPOAuthError: the operator redirect base is unset/insecure (fail-closed).
    """
    ctx = mcp_store.oauth_server_context(rls_engine=rls_engine, config=config, server_id=server_id)
    provider_key = ctx["oauth_provider"]
    if not provider_key:
        raise MCPOAuthProviderError(
            "server is not configured for oauth", context={"reason": "not_oauth"}
        )
    provider = await _resolve_provider(rls_engine=rls_engine, config=config, ctx=ctx)
    redirect_uri = callback_redirect_uri(config.mcp_oauth_redirect_base_url)
    minted = state_store.create_state(
        rls_engine=rls_engine,
        config=config,
        owner_id=ctx["owner_id"],
        server_id=server_id,
        provider=str(provider_key),
        redirect_after=redirect_after,
    )
    resource = ctx["url"] if provider.sends_resource_param else None
    return build_authorize_url(
        provider=provider,
        redirect_uri=redirect_uri,
        state=minted.state,
        code_challenge=minted.pkce.challenge,
        resource=resource,
    )


def _expiry_from(expires_in: int | None) -> datetime | None:
    if expires_in is None:
        return None
    return datetime.now(UTC) + timedelta(seconds=expires_in)


def _persist_tokens(
    *,
    rls_engine: Engine,
    config: APIConfig,
    server_id: str,
    provider_key: str,
    tokens: TokenResponse,
) -> None:
    mcp_store.store_oauth_tokens(
        rls_engine=rls_engine,
        config=config,
        server_id=server_id,
        provider=provider_key,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        access_token_expires_at=_expiry_from(tokens.expires_in),
        scopes=tokens.scope,
    )


async def handle_callback(
    *,
    rls_engine: Engine,
    config: APIConfig,
    state: str,
    code: str,
) -> CallbackResult:
    """Consume state, exchange the code for tokens, persist them. Returns the result.

    Fail-closed: a bad/expired/foreign state (:class:`MCPOAuthStateError`) or an
    exchange failure (:class:`MCPOAuthError`) leaves the server not connected — no
    token is written. RLS-scoped: ``consume_state`` runs under the caller's engine so
    a stolen victim state is invisible.
    """
    rec = state_store.consume_state(rls_engine=rls_engine, config=config, state=state)
    ctx = mcp_store.oauth_server_context(
        rls_engine=rls_engine, config=config, server_id=rec.server_id
    )
    provider = await _resolve_provider(rls_engine=rls_engine, config=config, ctx=ctx)
    redirect_uri = callback_redirect_uri(config.mcp_oauth_redirect_base_url)
    resource = ctx["url"] if provider.sends_resource_param else None
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        tokens = await exchange_code(
            provider=provider,
            redirect_uri=redirect_uri,
            code=code,
            code_verifier=rec.code_verifier,
            http_client=client,
            resource=resource,
        )
    _persist_tokens(
        rls_engine=rls_engine,
        config=config,
        server_id=rec.server_id,
        provider_key=rec.provider,
        tokens=tokens,
    )
    return CallbackResult(server_id=rec.server_id, redirect_after=rec.redirect_after)


def _needs_refresh(expires_at: datetime | None, leeway_seconds: int) -> bool:
    """True iff a known expiry is within the leeway window (proactive refresh).

    Unknown expiry (``None``) → skip proactive refresh (rely on reconnect-on-401, T8).
    """
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC) + timedelta(seconds=leeway_seconds)


async def refresh_persona_oauth_servers(
    *, rls_engine: Engine, config: APIConfig, persona_id: str
) -> None:
    """Refresh-before-inject (R8-D-5): rotate any near-expiry token before the header build.

    Runs ahead of ``_build_byo_mcp_clients`` in the runtime factory. For each of the
    persona's enabled oauth servers whose access token is within the refresh leeway
    and which holds a refresh token, refresh + rotate + persist atomically. A hard
    failure clears the server's tokens (fail-closed → not connected + reconnect
    prompt), never a stale/partial write. Best-effort: one server's failure never
    blocks the others or the toolbox build.
    """
    servers = mcp_store.oauth_servers_for_persona(
        rls_engine=rls_engine, config=config, persona_id=persona_id
    )
    leeway = config.mcp_oauth_refresh_leeway_seconds
    for s in servers:
        if not _needs_refresh(s["access_token_expires_at"], leeway):
            continue
        if not s["refresh_token"]:
            # Near expiry but no refresh token — cannot refresh; leave for reconnect-on-401.
            continue
        await _refresh_one(rls_engine=rls_engine, config=config, server=s)


async def reauthenticate_server(
    *, rls_engine: Engine, config: APIConfig, server_id: str
) -> dict[str, str] | None:
    """Reconnect-on-401 callback (R8-D-5): refresh+rotate → fresh bearer header, or None.

    Invoked by :class:`MCPClient` when a mid-session request 401s. Loads the server
    (RLS-scoped), refreshes + rotates + persists the token, and returns the new
    ``{"Authorization": "Bearer <access>"}`` header for the client to rebuild with. On a
    hard failure it clears the tokens (fail-closed → not connected) and returns ``None``
    so the client leaves the server disconnected — never a retry loop.
    """
    try:
        ctx = mcp_store.oauth_server_context(
            rls_engine=rls_engine, config=config, server_id=server_id
        )
    except Exception:  # noqa: BLE001 — a vanished/foreign server → fail closed, no header
        return None
    if not ctx.get("refresh_token") or not ctx.get("oauth_provider"):
        return None
    try:
        provider = await _resolve_provider(rls_engine=rls_engine, config=config, ctx=ctx)
        resource = str(ctx["url"]) if provider.sends_resource_param else None
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            tokens = await refresh_access_token(
                provider=provider,
                refresh_token=str(ctx["refresh_token"]),
                http_client=client,
                resource=resource,
            )
    except MCPOAuthError:
        _log.warning("mcp oauth reauth failed; dropping server to not-connected")
        mcp_store.clear_oauth_tokens(rls_engine=rls_engine, server_id=server_id)
        return None
    _persist_tokens(
        rls_engine=rls_engine,
        config=config,
        server_id=server_id,
        provider_key=str(ctx["oauth_provider"]),
        tokens=tokens,
    )
    return {"Authorization": f"Bearer {tokens.access_token}"}


async def _refresh_one(*, rls_engine: Engine, config: APIConfig, server: dict[str, object]) -> None:
    server_id = str(server["id"])
    try:
        provider = await _resolve_provider(rls_engine=rls_engine, config=config, ctx=server)
        resource = str(server["url"]) if provider.sends_resource_param else None
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            tokens = await refresh_access_token(
                provider=provider,
                refresh_token=str(server["refresh_token"]),
                http_client=client,
                resource=resource,
            )
    except MCPOAuthError:
        # Fail-closed: the rotating refresh token is spent + the exchange failed →
        # drop to not-connected so the user re-authorizes (never a silent stale token).
        _log.warning("mcp oauth refresh failed; dropping server to not-connected")
        mcp_store.clear_oauth_tokens(rls_engine=rls_engine, server_id=server_id)
        return
    _persist_tokens(
        rls_engine=rls_engine,
        config=config,
        server_id=server_id,
        provider_key=str(server["oauth_provider"]),
        tokens=tokens,
    )
