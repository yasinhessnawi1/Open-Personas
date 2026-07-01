"""Tools + skills read-only endpoints (spec 08, T13, §5.4).

``GET /v1/tools`` and ``GET /v1/skills`` — platform-global name+description
lists for the web authoring flow's checkboxes. Authenticated (consistent with
the rest of the surface) but not RLS-scoped (no tenant data).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from persona.tools.mcp.catalog import recommender_provider_tag

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.schemas import (
    MCPCatalogSecret,
    MCPCatalogServer,
    SpecialitySummary,
    ToolSummary,
)
from persona_api.services import catalog_service

router = APIRouter(prefix="/v1", tags=["catalog"])

__all__ = ["router"]


@router.get("/tools", response_model=list[ToolSummary])
async def list_tools(
    _user: AuthenticatedUser = Depends(get_current_user),
) -> list[ToolSummary]:
    """List the available tools (name + description)."""
    return [ToolSummary(name=n, description=d) for n, d in catalog_service.list_tools()]


@router.get("/skills", response_model=list[ToolSummary])
async def list_skills(
    _user: AuthenticatedUser = Depends(get_current_user),
) -> list[ToolSummary]:
    """List the available skills (name + description)."""
    return [ToolSummary(name=n, description=d) for n, d in catalog_service.list_skills()]


@router.get("/specialities", response_model=list[SpecialitySummary])
async def list_specialities(
    _user: AuthenticatedUser = Depends(get_current_user),
) -> list[SpecialitySummary]:
    """List the available specialities (skills) with trust tier + content hash (Spec S3).

    The catalog-level surface — the builtin floor + S2's synced external tiers — carrying
    each skill's source-assigned ``trust`` (S1-D-3), whether it ``requires_consent``
    (S1-D-4), and its ``content_hash`` (the version consent binds to, S1-D-5). The
    new-persona flow reads this (no persona context yet); the edit flow reads the
    persona-scoped variant that adds the per-persona declared + consent state.
    """
    return [
        SpecialitySummary(
            name=s.name,
            description=s.description,
            when_to_use=s.when_to_use,
            trust=s.trust.value,
            requires_consent=s.trust.requires_consent,
            content_hash=s.provenance.content_hash if s.provenance else None,
            source=s.provenance.source if s.provenance else None,
            source_uri=s.provenance.source_uri if s.provenance else None,
            source_ref=s.provenance.source_ref if s.provenance else None,
        )
        for s in catalog_service.list_specialities()
    ]


@router.get("/mcp-catalog", response_model=list[MCPCatalogServer])
async def list_mcp_catalog(
    _user: AuthenticatedUser = Depends(get_current_user),
) -> list[MCPCatalogServer]:
    """List the MCP catalog (builtin floor + Docker mirror; spec 30 T11 + N1).

    The mirror's display metadata + credential schema ride additive fields; the
    secret schema is display-only (no value, D-N1-5).
    """
    return [
        MCPCatalogServer(
            name=e.name,
            description=e.description,
            provider=recommender_provider_tag(e),
            default_enabled=e.default_enabled,
            required_env=list(e.required_env),
            display_name=e.display_name,
            icon_url=e.icon_url,
            image=e.image,
            server_type=e.server_type,
            risk=e.risk,
            source_project=e.source_project,
            source_commit=e.source_commit,
            signed=e.signed,
            allow_hosts=list(e.allow_hosts),
            secrets=[
                MCPCatalogSecret(
                    name=s.name, env=s.env, example=s.example, description=s.description
                )
                for s in e.secrets
            ],
        )
        for e in catalog_service.merged_mcp_catalog()
    ]
