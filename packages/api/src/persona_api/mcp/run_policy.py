"""The vetted-set policy for images permitted to RUN per-tenant (Spec N6, N6-D-4).

The single edition-aware gate answering "which catalog images may run per-tenant?" —
distinct from :mod:`persona_api.mcp.adoption_policy` (which governs *remote* adoption).
It runs third-party container code per tenant, so the basis is deliberately conservative
and **not signature-based** (the mirror carries no image signatures, N6-R-1):

- **basis** — the entry is an image server (``server_type == "server"``), its image is in
  Docker's official ``mcp/`` namespace, and it carries provenance (``source_commit``);
- **community** — any image meeting the basis (the operator owns the trust choice). *In
  practice the per-tenant Fly runtime is not wired in community (N6-D-5); this branch keeps
  the policy consistent with* :mod:`adoption_policy`;
- **cloud** — only images ALSO in the operator allow-list (``PERSONA_MCP_RUN_VETTED``); the
  **empty default is deny-all (fail-closed)** — nothing runs per-tenant until vetted.

Checked at BOTH the assign boundary (the enable path, T5) and the spawn boundary (the
runtime's ``ensure``, T4) — the TOCTOU guard: an image vetted at assign but pulled from the
allow-list before spawn fails closed at spawn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_api.config import Edition

if TYPE_CHECKING:
    from collections.abc import Iterable

    from persona.tools.mcp.catalog import MCPCatalog, MCPServerCatalogEntry

__all__ = [
    "is_runnable",
    "runnable_catalog_names",
    "runnable_images",
]

#: Docker's official, curated image namespace (89% of the catalog; N6-R-1). The honest
#: trust basis alongside provenance + the operator allow-list — NOT signatures.
_OFFICIAL_NAMESPACE = "mcp/"


def _meets_basis(entry: MCPServerCatalogEntry) -> bool:
    """The namespace + provenance + image-server basis (edition-independent)."""
    return (
        entry.server_type == "server"
        and entry.image.startswith(_OFFICIAL_NAMESPACE)
        and bool(entry.source_commit)
    )


def runnable_catalog_names(
    *, edition: Edition, vetted: Iterable[str], catalog: MCPCatalog
) -> frozenset[str]:
    """The catalog entry names that may RUN per-tenant in this edition (N6-D-4).

    Community returns every basis-meeting image (user owns trust); cloud returns only those
    ALSO in ``vetted`` (empty allow-list → empty set, fail-closed).
    """
    base = frozenset(name for name, e in catalog.servers.items() if _meets_basis(e))
    if edition is Edition.community:
        return base
    return base & frozenset(vetted)


def is_runnable(name: str, *, edition: Edition, vetted: Iterable[str], catalog: MCPCatalog) -> bool:
    """Whether ``name`` may run per-tenant in this edition (the assign-boundary check)."""
    return name in runnable_catalog_names(edition=edition, vetted=vetted, catalog=catalog)


def runnable_images(
    *, edition: Edition, vetted: Iterable[str], catalog: MCPCatalog
) -> frozenset[str]:
    """The image refs of the runnable entries — the spawn-boundary check set.

    The runtime's ``ensure`` holds an image ref (not a catalog name), so it gates on this
    set: ``image in runnable_images(...)``. Recomputing it at spawn (from the CURRENT
    allow-list) is what makes the TOCTOU guard fail closed when an image is de-vetted
    between assign and spawn.
    """
    names = runnable_catalog_names(edition=edition, vetted=vetted, catalog=catalog)
    return frozenset(catalog.servers[n].image for n in names)
