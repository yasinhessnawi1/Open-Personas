"""Connector management routes (Spec C6) — the authenticated web front-door.

The C1-designated "C6 backend" (D-C1-5, C6-D-0): the thin authenticated surface
the web app drives to list + manage connector bindings. ``GET`` (here) is
RLS-native on persona-api's own ``connector_identities`` table; unlink (T2) and
the issue-proxy (T3) follow. No linking logic lives here — that is C1's spine +
the connector service's per-platform carriers; this is a management surface over
existing capability.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — used in cast() at runtime
from enum import StrEnum
from typing import cast

import httpx
from fastapi import APIRouter, Depends, Request

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.errors import ConnectorServiceUnavailableError
from persona_api.schemas import (
    ConnectorConnectionOut,
    ConnectorDisconnectResult,
    ConnectorLinkArtifact,
)
from persona_api.services import connectors_service

router = APIRouter(prefix="/v1/me/connectors", tags=["connectors"])

# The link-initiation proxy budget: link issue is a single fast upstream call; a tight
# timeout means the surface fails soft quickly (never a dead spinner) if the connector
# service hangs, rather than holding the request open.
_PROXY_TIMEOUT = httpx.Timeout(10.0)


class ConnectorPlatform(StrEnum):
    """The six linkable platforms — a closed set (C6-D-1).

    Used as the ``link`` path-param type so FastAPI rejects any other value with a 422
    BEFORE it can reach the proxy: the value is interpolated into the upstream URL path,
    so a closed enum forecloses path-injection / SSRF into other connector-service routes.
    """

    telegram = "telegram"
    discord = "discord"
    slack = "slack"
    whatsapp = "whatsapp"
    sms = "sms"
    email = "email"


@router.get("", response_model=list[ConnectorConnectionOut])
async def list_connectors(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> list[ConnectorConnectionOut]:
    """The caller's active platform connections (Spec C6, RLS-scoped).

    Only the caller's OWN active bindings (criterion 11); absence of a platform
    from the list ⇒ not connected. The web merges this against its static
    six-platform catalogue to render connected / not-connected state + the
    connected identity.
    """
    rows = connectors_service.list_connections(rls_engine=request.app.state.rls_engine)
    return [
        ConnectorConnectionOut(
            platform=str(r["platform"]),
            platform_identity=str(r["platform_identity"]),
            linked_at=cast("datetime", r["linked_at"]),
        )
        for r in rows
    ]


@router.delete("/{platform}/{platform_identity}", response_model=ConnectorDisconnectResult)
async def disconnect_connector(
    platform: str,
    platform_identity: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> ConnectorDisconnectResult:
    """Sever the caller's binding for ``(platform, platform_identity)`` (Spec C6).

    Drives C1's real unlink (``revoke_identity``) under RLS — the platform stops
    reaching the caller's personas (criterion 9), not just a greyed UI chip.
    Idempotent: ``severed=false`` when there was no active binding of the caller's
    to sever (already disconnected / never existed / not owned — RLS makes a
    foreign binding a no-op, never a leaking 404). ``platform_identity`` arrives
    URL-encoded (phone ``+E164`` / email / Slack ``team:user``); FastAPI decodes it.
    """
    severed = connectors_service.revoke_connection(
        rls_engine=request.app.state.rls_engine,
        platform=platform,
        platform_identity=platform_identity,
    )
    return ConnectorDisconnectResult(severed=severed > 0)


@router.post("/{platform}/link", response_model=ConnectorLinkArtifact)
async def initiate_link(
    platform: ConnectorPlatform,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — auth wall; owner in bearer
) -> ConnectorLinkArtifact:
    """Initiate a platform link — proxy to the connector service (Spec C6, C6-D-0).

    The web's single front-door for link initiation. persona-api does not issue tokens
    (no linking logic here); it forwards to the separate connector service, which owns the
    per-platform carriers + platform secrets. **The owner crosses the boundary as the
    verified Clerk bearer, never a parameter** — the connector service re-verifies the same
    token and derives the owner from its ``sub``, so no ``owner_id`` is spoofable (an
    attacker hitting the connector service directly can still only mint for their own sub).

    Fails soft: an unset ``connector_service_url``, an unreachable service, a timeout, or a
    non-2xx upstream all raise :class:`ConnectorServiceUnavailableError` (503) so the surface
    shows "temporarily unavailable" — never a dead spinner. Returns exactly the normalized
    :class:`ConnectorLinkArtifact` (``extra="forbid"``) so no upstream field leaks.
    """
    base = request.app.state.config.connector_service_url.rstrip("/")
    if not base:
        raise ConnectorServiceUnavailableError(
            "the connector service is not configured", context={"platform": platform.value}
        )
    # Forward the caller's verified bearer for the connector service to re-verify (the owner
    # is the token's sub, never a body param). Community has no bearer → the upstream rejects
    # and we fail soft, consistent with the honest-unavailable posture.
    authorization = request.headers.get("Authorization")
    headers = {"Authorization": authorization} if authorization else {}
    url = f"{base}/v1/connectors/{platform.value}/link"
    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as client:
            resp = await client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        raise ConnectorServiceUnavailableError(
            "the connector service is unreachable", context={"platform": platform.value}
        ) from exc
    if resp.status_code != httpx.codes.OK:
        # Any non-200 (incl. an upstream 401 on a config mismatch, or 404 for an
        # unconfigured platform) is surfaced as a single honest "unavailable" — the web
        # never sees an upstream status, and no oracle distinguishes the sub-reason.
        raise ConnectorServiceUnavailableError(
            "the connector service could not issue a link", context={"platform": platform.value}
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise ConnectorServiceUnavailableError(
            "the connector service returned a malformed response",
            context={"platform": platform.value},
        ) from exc
    # Copy ONLY the known keys — an unexpected upstream field is dropped, not leaked or
    # errored (extra="forbid" on the schema closes the OpenAPI contract too).
    try:
        return ConnectorLinkArtifact(
            deep_link=data.get("deep_link"),
            authorize_url=data.get("authorize_url"),
            code=data.get("code"),
            destination=data.get("destination"),
            expires_at=data["expires_at"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ConnectorServiceUnavailableError(
            "the connector service returned an unexpected link shape",
            context={"platform": platform.value},
        ) from exc


__all__ = ["router"]
