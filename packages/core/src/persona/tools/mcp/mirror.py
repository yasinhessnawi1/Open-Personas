"""Docker MCP catalog-mirror loader — request-path, zero-network (Spec N1, D-N1-4).

Loads the bundled ``mirror.json`` snapshot (the offline-synced mirror of
``github.com/docker/mcp-registry``; see :mod:`persona.tools.mcp.mirror_sync`) and
exposes it as an :class:`~persona.tools.mcp.catalog.MCPCatalog`. Resolution is 100%
local — exactly like the builtin ``catalog.toml`` load (D-03-22, the §9.4 precursor)
— so request-time tool resolution never touches the network.

**Fail-soft is the load-bearing property (D-N1-4):** the snapshot is optional and may
be absent (no sync run yet), stale, or corrupt. This loader NEVER raises at boot — a
missing / unreadable / structurally-invalid snapshot falls back to the bundled builtin
catalog (``catalog.toml``). The mirror and the Gateway connection (D-N1-1) are
independent features: you can connect with no mirror, and mirror with no gateway.

This module is deliberately git-/yaml-free (the network-touching sync lives in
:mod:`persona.tools.mcp.mirror_sync`, never reachable from the request path).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from persona.logging import get_logger
from persona.tools.mcp.catalog import (
    BUILTIN_MCP_CATALOG,
    MCPCatalog,
    MCPServerCatalogEntry,
)

__all__ = ["MIRROR_PATH", "load_mirror_catalog", "resolve_mirror_write_path"]

_log = get_logger("tools.mcp.mirror")

#: The bundled snapshot, written by the offline sync as package data beside
#: ``catalog.toml``. Absent until a sync has run (fail-soft → builtin catalog). On a
#: deployed image this path is root-owned ``/app/...`` (baked-in, lost on redeploy), so
#: the auto-sync (N2) writes to a writable ``override`` on the mounted volume instead and
#: this stays the read-time fallback only (N2-D-1).
MIRROR_PATH: Path = Path(__file__).parent / "mirror.json"


def _try_load(path: Path) -> MCPCatalog | None:
    """Load one snapshot file, or ``None`` if absent / unreadable / structurally invalid.

    Never raises — every failure mode is caught + logged so a malformed snapshot can never
    break boot (D-N1-4). ``None`` is the signal to fall through to the next candidate.
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        servers: dict[str, MCPServerCatalogEntry] = {}
        for item in raw["servers"]:
            entry = MCPServerCatalogEntry.model_validate(item)
            servers[entry.name] = entry
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
        _log.warning(
            "mcp mirror snapshot unreadable; trying the next candidate",
            path=str(path),
            error=type(exc).__name__,
        )
        return None
    _log.info("mcp mirror snapshot loaded", path=str(path), server_count=len(servers))
    return MCPCatalog(servers=servers)


def load_mirror_catalog(
    path: Path = MIRROR_PATH,
    *,
    override: Path | None = None,
    fallback: MCPCatalog = BUILTIN_MCP_CATALOG,
) -> MCPCatalog:
    """Load the mirror snapshot, or fall back to the builtin catalog (never raises).

    The precedence (N2-D-1, extending D-N1-4's fail-soft chain): the auto-synced
    ``override`` (the writable mirror on the mounted volume, when set + valid) → the bundled
    ``path`` snapshot (when valid) → ``fallback`` (the builtin ``catalog.toml``). Every
    fall-through is fail-soft: a missing / unreadable / structurally-invalid candidate is
    skipped (logged), never raised — so a corrupt override does NOT skip straight to the
    builtin, it tries the bundled snapshot first.

    Args:
        path: The bundled snapshot file (defaults to the package-data :data:`MIRROR_PATH`).
        override: The writable, operator-configured snapshot (``PERSONA_MCP_MIRROR_PATH``);
            tried first when set. ``None`` (unset) → start from ``path``.
        fallback: The catalog returned when no snapshot is usable (defaults to the builtin
            ``catalog.toml`` catalog).

    Returns:
        The first usable parsed mirror catalog, or ``fallback``.
    """
    for candidate in (override, path):
        if candidate is None:
            continue
        catalog = _try_load(candidate)
        if catalog is not None:
            return catalog
    _log.info("mcp mirror snapshot absent; using builtin catalog")
    return fallback


def resolve_mirror_write_path(override: Path | None) -> Path | None:
    """The path the auto-sync writes the reconciled snapshot to, or ``None`` (N2-D-1).

    The configured ``override`` (``PERSONA_MCP_MIRROR_PATH`` — the writable mirror on the
    mounted volume) when set, else ``None`` — the sync must then SKIP (warn), never write.
    The bundled :data:`MIRROR_PATH` is committed package data and a **read-time fallback
    only** (N2-D-1: "the bundled file stays the read-time fallback"): on a deployed image
    it is root-owned + lost on redeploy, and in a dev/test checkout writing it dirties the
    git tree with a random-day snapshot (R9-011). Writes therefore REQUIRE an explicit
    target; availability fail-softs to the bundled snapshot via :func:`load_mirror_catalog`.
    """
    return override
