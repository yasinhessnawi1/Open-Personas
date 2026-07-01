"""Bring-your-own MCP server routes (spec 30 T09, D-30-3/4/5/6).

RLS-scoped CRUD + test-connection/discovery for user-owned MCP servers, plus
persona↔server assignment. The user-supplied URL is SSRF-validated at every
mutation (and resolve-then-pinned on the live connect, in the store/runtime);
credentials are encrypted at rest and NEVER returned (only ``has_credential``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.mcp import store as mcp_store
from persona_api.mcp.oauth import service as oauth_service
from persona_api.middleware.rate_limit import rate_limit
from persona_api.schemas import (
    AdoptCatalogAppRequest,
    CreateMCPServerRequest,
    MCPOAuthAuthorizeRequest,
    MCPOAuthAuthorizeResponse,
    MCPOAuthCallbackRequest,
    MCPOAuthCallbackResponse,
    MCPServerDetail,
    MCPServerTestResult,
    UpdateMCPServerRequest,
)
from persona_api.services import adoption_service, audit_service

router = APIRouter(prefix="/v1", tags=["mcp"])

__all__ = ["router"]


@router.post(
    "/mcp-servers",
    response_model=MCPServerDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("default"))],
)
async def create_mcp_server(
    body: CreateMCPServerRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MCPServerDetail:
    """Add a bring-your-own MCP server (SSRF-validated; credential encrypted)."""
    detail = mcp_store.create_server(
        rls_engine=request.app.state.rls_engine,
        config=request.app.state.config,
        owner_id=user.id,
        name=body.name,
        url=body.url,
        auth_method=body.auth_method,
        credential=body.credential,
        oauth_provider=body.oauth_provider,
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="mcp.server_add",
        target=detail["id"],
    )
    return MCPServerDetail(**detail)


@router.post(
    "/mcp-servers/{server_id}/oauth/authorize",
    response_model=MCPOAuthAuthorizeResponse,
    dependencies=[Depends(rate_limit("default"))],
)
async def start_mcp_oauth(
    server_id: str,
    body: MCPOAuthAuthorizeRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MCPOAuthAuthorizeResponse:
    """Begin the OAuth flow for a BYO MCP server (Spec R8, T4).

    RLS-scoped (the server must be the caller's → 404). Mints a server-side
    state + PKCE pair and returns the provider authorize URL (challenge + opaque
    state on it — no secret). ``redirect_after`` is stored against the state.
    """
    authorize_url = await oauth_service.initiate_authorize(
        rls_engine=request.app.state.rls_engine,
        config=request.app.state.config,
        server_id=server_id,
        redirect_after=body.redirect_after,
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="mcp.oauth_start",
        target=server_id,
    )
    return MCPOAuthAuthorizeResponse(authorize_url=authorize_url)


@router.post(
    "/mcp-servers/oauth/callback",
    response_model=MCPOAuthCallbackResponse,
    dependencies=[Depends(rate_limit("default"))],
)
async def complete_mcp_oauth(
    body: MCPOAuthCallbackRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MCPOAuthCallbackResponse:
    """Complete the OAuth flow (Spec R8, T5): consume state, exchange code, store tokens.

    The authenticated web callback relays ``state`` + ``code``. ``consume_state`` runs
    RLS-scoped to the caller (a stolen/foreign/expired state → fail-closed 400); the
    code is exchanged on the back channel and the tokens are persisted encrypted. No
    token is ever returned. Fail-closed on any error — the server stays not connected.
    """
    result = await oauth_service.handle_callback(
        rls_engine=request.app.state.rls_engine,
        config=request.app.state.config,
        state=body.state,
        code=body.code,
    )
    # The server_id is bound to the consumed state (not the request body). Re-read the
    # now-connected server (RLS-scoped) for the client.
    detail = mcp_store.get_server(
        rls_engine=request.app.state.rls_engine, server_id=result.server_id
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="mcp.oauth_complete",
        target=result.server_id,
    )
    return MCPOAuthCallbackResponse(
        server=MCPServerDetail(**detail), redirect_after=result.redirect_after
    )


@router.get("/mcp-servers", response_model=list[MCPServerDetail])
async def list_mcp_servers(
    request: Request,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> list[MCPServerDetail]:
    """List the caller's BYO MCP servers (credential redacted)."""
    return [
        MCPServerDetail(**d)
        for d in mcp_store.list_servers(rls_engine=request.app.state.rls_engine)
    ]


@router.get("/mcp-servers/{server_id}", response_model=MCPServerDetail)
async def get_mcp_server(
    server_id: str,
    request: Request,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> MCPServerDetail:
    """Get one BYO MCP server (RLS-scoped → 404)."""
    return MCPServerDetail(
        **mcp_store.get_server(rls_engine=request.app.state.rls_engine, server_id=server_id)
    )


@router.patch(
    "/mcp-servers/{server_id}",
    response_model=MCPServerDetail,
    dependencies=[Depends(rate_limit("default"))],
)
async def update_mcp_server(
    server_id: str,
    body: UpdateMCPServerRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MCPServerDetail:
    """Patch a BYO MCP server (re-validates a new URL; re-encrypts credentials)."""
    detail = mcp_store.update_server(
        rls_engine=request.app.state.rls_engine,
        config=request.app.state.config,
        server_id=server_id,
        name=body.name,
        url=body.url,
        auth_method=body.auth_method,
        credential=body.credential,
        enabled=body.enabled,
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="mcp.server_update",
        target=server_id,
    )
    return MCPServerDetail(**detail)


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Delete a BYO MCP server (assignments cascade; RLS-scoped → 404)."""
    mcp_store.delete_server(rls_engine=request.app.state.rls_engine, server_id=server_id)
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="mcp.server_delete",
        target=server_id,
    )


@router.post(
    "/mcp-servers/{server_id}/test",
    response_model=MCPServerTestResult,
    dependencies=[Depends(rate_limit("default"))],
)
async def check_mcp_server_connection(
    server_id: str,
    request: Request,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> MCPServerTestResult:
    """Test-connect to the server (SSRF-pinned) and discover its tools (D-30-5)."""
    result = await mcp_store.test_connection(
        rls_engine=request.app.state.rls_engine,
        config=request.app.state.config,
        server_id=server_id,
    )
    return MCPServerTestResult(**result)


@router.get(
    "/personas/{persona_id}/mcp-servers",
    response_model=list[MCPServerDetail],
)
async def list_persona_mcp_servers(
    persona_id: str,
    request: Request,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> list[MCPServerDetail]:
    """List the BYO MCP servers assigned to a persona (RLS-scoped)."""
    return [
        MCPServerDetail(**d)
        for d in mcp_store.list_servers_for_persona(
            rls_engine=request.app.state.rls_engine, persona_id=persona_id
        )
    ]


@router.put(
    "/personas/{persona_id}/mcp-servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit("default"))],
)
async def assign_mcp_server(
    persona_id: str,
    server_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Assign a BYO MCP server to a persona (D-30-6; idempotent; RLS both ends)."""
    mcp_store.assign_to_persona(
        rls_engine=request.app.state.rls_engine,
        persona_id=persona_id,
        server_id=server_id,
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="mcp.server_assign",
        target=f"{persona_id}:{server_id}",
    )


@router.post(
    "/personas/{persona_id}/adopted-apps",
    response_model=MCPServerDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("default"))],
)
async def adopt_catalog_app(
    persona_id: str,
    body: AdoptCatalogAppRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MCPServerDetail:
    """Self-adopt a catalog app for a persona (Spec N4, B2-③).

    Owner-scoped (the persona must be the caller's → 404) and vetted (N4-D-6 → 403),
    both BEFORE any write. The connection url/auth are derived from the catalog entry
    (N4-D-10); the caller supplies only ``credential`` (a ``repr=False`` field, encrypted
    at rest, never returned/logged). The audit records name + provenance only.
    """
    detail = adoption_service.adopt_catalog_app(
        rls_engine=request.app.state.rls_engine,
        config=request.app.state.config,
        owner_id=user.id,
        persona_id=persona_id,
        catalog_name=body.catalog_name,
        credential=body.credential,
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="mcp.app_adopt",
        target=f"{persona_id}:{detail['catalog_source']}",
    )
    return MCPServerDetail(**detail)


@router.delete(
    "/personas/{persona_id}/mcp-servers/{server_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unassign_mcp_server(
    persona_id: str,
    server_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Remove a persona↔server assignment (idempotent; RLS-scoped)."""
    mcp_store.unassign_from_persona(
        rls_engine=request.app.state.rls_engine,
        persona_id=persona_id,
        server_id=server_id,
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="mcp.server_unassign",
        target=f"{persona_id}:{server_id}",
    )
