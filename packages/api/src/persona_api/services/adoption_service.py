"""Catalog-app self-adoption orchestration (Spec N4, B2-③).

The credential-isolated adopt flow a persona drives via ``mcp_search`` → propose → setup.
Reuses the Spec-30 store (N4-D-1) for the per-user credential; the route is a thin wrapper
over this service so the security spine is testable at the store level.

Order is security-load-bearing — every check runs BEFORE any write (fail-closed):

1. **owner-scoped authz** — the persona must be the caller's (``get_persona`` is RLS-scoped
   → ``PersonaNotFoundError`` → 404 if not), so a cross-tenant adopt writes nothing;
2. **vetted gate** (N4-D-6) — ``is_adoptable`` against the merged catalog; a non-remote /
   cloud-unvetted app → ``MCPAppNotAdoptableError`` → 403, nothing written;
3. **double-adopt** — a server already named for this app → ``MCPAppAlreadyAdoptedError`` →
   409 (a clear conflict, not a 500);
4. **derive + write** — ``url`` = the entry's ``remote_url`` and ``auth_method`` = bearer iff
   the entry declares a secret (both from the CATALOG, N4-D-10 — never the caller); the
   ``credential`` (from the caller) is encrypted by ``create_server`` and the row is tagged
   ``catalog_source``; then assigned to the persona.

The credential never reaches model context, logs, audit, or the response — it rides a
``repr=False`` request field, is encrypted at rest, and ``create_server`` returns only the
redacted detail (``has_credential``). The audit (in the route) carries name + provenance only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona.tools.mcp.catalog import MCPCatalog

from persona_api.config import Edition
from persona_api.errors import (
    MCPAppAlreadyAdoptedError,
    MCPAppNotAdoptableError,
    MCPRuntimeCapacityError,
)
from persona_api.mcp import store as mcp_store
from persona_api.mcp.adoption_policy import is_adoptable
from persona_api.mcp.run_policy import is_runnable
from persona_api.services import catalog_service, persona_service

if TYPE_CHECKING:
    from persona.tools.mcp.catalog import MCPServerCatalogEntry
    from sqlalchemy.engine import Engine

    from persona_api.config import APIConfig

__all__ = ["adopt_catalog_app"]


def adopt_catalog_app(
    *,
    rls_engine: Engine,
    config: APIConfig,
    owner_id: str,
    persona_id: str,
    catalog_name: str,
    credential: str | None,
    max_image_runtime: int = 3,
) -> dict[str, Any]:
    """Adopt a catalog app for a persona (the B2-③ orchestration). Returns the redacted detail.

    ONE grant path (N4-D-5) that branches on the entry's runtime: a ``type: remote`` app takes
    the N4 remote path (``is_adoptable`` → ``create_server`` with the catalog URL); an image
    server (``server_type == "server"``) takes the N6 path (``is_runnable`` re-check + the
    per-tenant cap + ``create_image_server`` — no remote URL). Every check runs BEFORE any
    write (fail-closed).

    Raises:
        PersonaNotFoundError: the persona is not the caller's (→ 404). Nothing written.
        MCPAppNotAdoptableError: the vetted/runnable gate refused the app (→ 403). Nothing written.
        MCPRuntimeCapacityError: the tenant is at its per-tenant image-runtime cap (→ 409).
        MCPAppAlreadyAdoptedError: the caller already has a server by this app's name (→ 409).
        MCPServerValidationError: the app declares a secret but no credential was supplied.
    """
    # 1. owner-scoped authz — RLS-scoped read; raises PersonaNotFoundError if not the caller's.
    persona_service.get_persona(rls_engine=rls_engine, persona_id=persona_id)

    catalog = MCPCatalog(servers={e.name: e for e in catalog_service.merged_mcp_catalog()})
    entry = catalog.servers.get(catalog_name)
    if entry is None:
        raise MCPAppNotAdoptableError(
            "unknown app", context={"app": catalog_name, "reason": "not in catalog"}
        )

    # 2. double-adopt — a clear conflict, never a 500 (shared across both runtimes).
    if any(s["name"] == catalog_name for s in mcp_store.list_servers(rls_engine=rls_engine)):
        raise MCPAppAlreadyAdoptedError("app already adopted", context={"app": catalog_name})

    if entry.server_type == "server":
        return _adopt_image_app(
            rls_engine=rls_engine,
            config=config,
            owner_id=owner_id,
            persona_id=persona_id,
            entry=entry,
            catalog=catalog,
            credential=credential,
            max_image_runtime=max_image_runtime,
        )
    return _adopt_remote_app(
        rls_engine=rls_engine,
        config=config,
        owner_id=owner_id,
        persona_id=persona_id,
        entry=entry,
        catalog=catalog,
        credential=credential,
    )


def _adopt_remote_app(
    *,
    rls_engine: Engine,
    config: APIConfig,
    owner_id: str,
    persona_id: str,
    entry: MCPServerCatalogEntry,
    catalog: MCPCatalog,
    credential: str | None,
) -> dict[str, Any]:
    """The N4 remote path: vetted gate (N4-D-6) → catalog-derived url/auth → write + assign."""
    if not is_adoptable(
        entry.name, edition=config.edition, vetted=config.mcp_adopt_vetted_list, catalog=catalog
    ):
        reason = (
            "not in the operator-vetted set"
            if config.edition is Edition.cloud
            else "not a remote app available for adoption"
        )
        raise MCPAppNotAdoptableError(
            "app is not adoptable", context={"app": entry.name, "reason": reason}
        )
    detail = mcp_store.create_server(
        rls_engine=rls_engine,
        config=config,
        owner_id=owner_id,
        name=entry.name,
        url=entry.remote_url,
        auth_method="bearer" if entry.secrets else "none",
        credential=credential,
        catalog_source=entry.name,
    )
    mcp_store.assign_to_persona(
        rls_engine=rls_engine, persona_id=persona_id, server_id=detail["id"]
    )
    return detail


def _adopt_image_app(
    *,
    rls_engine: Engine,
    config: APIConfig,
    owner_id: str,
    persona_id: str,
    entry: MCPServerCatalogEntry,
    catalog: MCPCatalog,
    credential: str | None,
    max_image_runtime: int,
) -> dict[str, Any]:
    """The N6 image path: runnable re-check (N6-D-4 TOCTOU) → per-tenant cap → write + assign."""
    # 3a. runnable-image vetting RE-CHECKED at assign (N6-D-4 TOCTOU guard) — fail-closed.
    if not is_runnable(
        entry.name, edition=config.edition, vetted=config.mcp_run_vetted_list, catalog=catalog
    ):
        raise MCPAppNotAdoptableError(
            "image is not runnable per-tenant",
            context={"app": entry.name, "reason": "not in the operator-vetted runnable set"},
        )
    # 3b. per-tenant cap ENFORCED AT ASSIGN (N6-D-3) — the (N+1)th enable denied, never a
    #     silent spawn-storm. Counts the tenant's existing image-server rows.
    if mcp_store.count_image_servers_for_owner(rls_engine=rls_engine, owner_id=owner_id) >= (
        max_image_runtime
    ):
        raise MCPRuntimeCapacityError(
            "per-tenant image-runtime cap reached",
            context={
                "app": entry.name,
                "cap": str(max_image_runtime),
                "reason": "runtime_capacity",
            },
        )
    # 4. write the image-server row (no remote URL; the runtime supplies /mcp) + assign.
    detail = mcp_store.create_image_server(
        rls_engine=rls_engine,
        config=config,
        owner_id=owner_id,
        name=entry.name,
        image=entry.image,
        credential=credential,
        catalog_source=entry.name,
    )
    mcp_store.assign_to_persona(
        rls_engine=rls_engine, persona_id=persona_id, server_id=detail["id"]
    )
    return detail
