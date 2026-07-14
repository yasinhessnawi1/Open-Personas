"""Offline Docker MCP catalog-mirror sync (Spec N1 T2, D-N1-3 / D-N1-4).

Clones ``github.com/docker/mcp-registry`` (one ``servers/<name>/server.yaml`` per
server; there is NO stable catalog API), maps each into an
:class:`~persona.tools.mcp.catalog.MCPServerCatalogEntry`, and writes a ``mirror.json``
snapshot the request-path loader (:mod:`persona.tools.mcp.mirror`) reads zero-network.

**This module is OFFLINE-ONLY and must NEVER be reached from the request path** — it is
the one place git + network + YAML parsing live. The request-time invariant (D-N1-4) is
that tool resolution touches no network; periodic auto-refresh is a separate spec (N2).
Run it as a maintainer / CI step.

Resilience (D-N1-4):
- one malformed ``server.yaml`` is **skipped + logged**, never fatal to the whole sync;
- the snapshot is written **atomically** (temp file + ``os.replace``), so a clone/parse
  failure mid-sync leaves the existing snapshot intact — a failed sync keeps the last
  good mirror rather than truncating it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess  # noqa: S404 — git clone of a fixed repo; offline path
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from persona.logging import get_logger
from persona.tools.mcp.catalog import (
    MCPRiskLevel,
    MCPSecretField,
    MCPServerCatalogEntry,
    MCPServerType,
)

__all__ = [
    "REGISTRY_REPO",
    "build_entries_from_source",
    "build_mirror_entries",
    "parse_server_yaml",
    "sync_mirror",
    "write_mirror_atomic",
]

_log = get_logger("tools.mcp.mirror_sync")

#: The citable programmatic source — mirror via git clone/pull, not an API (D-N1-1).
REGISTRY_REPO = "https://github.com/docker/mcp-registry.git"


def _coerce_server_type(raw: object) -> MCPServerType:
    """Map a ``server.yaml`` ``type`` to the catalog taxonomy (default ``"server"``)."""
    return "remote" if raw == "remote" else "server"


def _derive_risk(secrets: Sequence[MCPSecretField]) -> MCPRiskLevel:
    """Coarse risk label for the apps UX (D-N1-3 micro-decision).

    A secret-requiring server reaches an external account (a GitHub token, a Slack
    token) — a larger blast radius — so it is labelled ``"medium"``; a secret-less
    server is ``"low"``. Execution isolation itself is the gateway's/Docker's concern
    (per-container caps, ``--block-network``); this is a credential-exposure signal,
    not an execution-risk verdict.
    """
    return "medium" if secrets else "low"


def parse_server_yaml(name: str, data: Mapping[str, Any]) -> MCPServerCatalogEntry | None:
    """Map one parsed ``server.yaml`` to a catalog entry, or ``None`` if malformed.

    ``Any`` is unavoidable here — the value comes from arbitrary external YAML; we
    narrow defensively and skip (return ``None``) on any structural surprise rather
    than abort the whole sync (D-N1-4).

    Args:
        name: The server name (the ``servers/<name>/`` directory).
        data: The ``yaml.safe_load`` of the server's ``server.yaml``.

    Returns:
        The mapped entry, or ``None`` when the YAML is structurally malformed.
    """
    try:
        about = data.get("about") or {}
        source = data.get("source") or {}
        run = data.get("run") or {}
        config = data.get("config") or {}
        remote = data.get("remote") or {}  # the hosted endpoint for type:remote servers (N4)
        secrets = tuple(
            MCPSecretField(
                name=str(s["name"]),
                env=str(s["env"]),
                example=str(s.get("example", "")),
                description=str(s.get("description", "")),
            )
            for s in (config.get("secrets") or [])
        )
        return MCPServerCatalogEntry.model_validate(
            {
                "name": name,
                "description": str(about.get("description", "")),
                "kind": "external",  # Persona ships no code for mirror servers
                "risk": _derive_risk(secrets),
                "display_name": str(about.get("title", "")),
                "icon_url": str(about.get("icon", "")),
                "image": str(data.get("image", "")),
                "server_type": _coerce_server_type(data.get("type")),
                "remote_url": str(remote.get("url", "")),
                "source_project": str(source.get("project", "")),
                "source_commit": str(source.get("commit", "")),
                "allow_hosts": tuple(str(h) for h in (run.get("allowHosts") or ())),
                "secrets": secrets,
            }
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        # ValueError covers pydantic ValidationError; the others cover bad YAML shapes
        # (e.g. `about` as a scalar). Skip + log — one bad server never fails the sync.
        _log.warning("skipping malformed server.yaml", name=name, error=type(exc).__name__)
        return None


def build_mirror_entries(registry_root: Path) -> list[MCPServerCatalogEntry]:
    """Walk a registry checkout's ``servers/*/server.yaml`` into catalog entries.

    Args:
        registry_root: The root of a ``docker/mcp-registry`` checkout (must contain a
            ``servers/`` directory).

    Returns:
        The successfully-parsed entries, in directory order. Malformed ``server.yaml``
        files are skipped + logged.

    Raises:
        FileNotFoundError: when ``registry_root/servers`` does not exist (a clone that
            produced nothing) — raised BEFORE any snapshot write, so the existing
            snapshot is preserved.
    """
    servers_dir = registry_root / "servers"
    if not servers_dir.is_dir():
        msg = f"no servers/ directory under {registry_root}"
        raise FileNotFoundError(msg)
    entries: list[MCPServerCatalogEntry] = []
    for server_yaml in sorted(servers_dir.glob("*/server.yaml")):
        server_name = server_yaml.parent.name
        try:
            data = yaml.safe_load(server_yaml.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            _log.warning(
                "skipping unreadable server.yaml", name=server_name, error=type(exc).__name__
            )
            continue
        if not isinstance(data, Mapping):
            _log.warning("skipping non-mapping server.yaml", name=server_name)
            continue
        entry = parse_server_yaml(server_name, data)
        if entry is not None:
            entries.append(entry)
    return entries


def write_mirror_atomic(entries: Sequence[MCPServerCatalogEntry], path: Path) -> None:
    """Write the snapshot atomically (temp file + ``os.replace``); D-N1-4.

    A partially-written file is never observable: we write a sibling temp file and
    atomically rename it over ``path`` only once fully written. On any failure the temp
    file is removed and the existing ``path`` is left untouched.
    """
    payload = {"version": 1, "servers": [e.model_dump(mode="json") for e in entries]}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".mirror.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        Path(tmp_name).replace(path)  # atomic on the same filesystem
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise


def _clone_registry(repo: str, dest: Path) -> None:
    """Shallow-clone the registry repo (offline-only path).

    R9-041: a runtime image missing ``git`` previously failed here with a bare
    ``FileNotFoundError`` from ``subprocess`` — accurate but opaque. One honest
    WARNING names the real cause before the (unchanged) attempt, so whoever
    reads the eventual sync failure (the caller's existing fail-soft catch —
    e.g. :class:`~persona_api.jobs.catalog_sync.CatalogSyncTask.run_once`)
    sees why, instead of decoding a raw ``[Errno 2]``.
    """
    if shutil.which("git") is None:
        _log.warning(
            "git not on PATH — MCP catalog mirror sync cannot clone; keeping the last-good mirror"
        )
    subprocess.run(  # noqa: S603 — fixed argv, no shell; repo is a constant/operator arg
        ["git", "clone", "--depth", "1", repo, str(dest)],  # noqa: S607 — git on PATH by design
        check=True,
        capture_output=True,
        text=True,
    )


def build_entries_from_source(
    *,
    repo: str = REGISTRY_REPO,
    registry_root: Path | None = None,
) -> list[MCPServerCatalogEntry]:
    """Obtain catalog entries from the registry — a local checkout or a fresh clone.

    The single shared pull seam (N1 + N2): when ``registry_root`` is given it reads
    that existing checkout (tests / local runs, no network); otherwise it shallow-clones
    ``repo`` into a temp dir and reads it. OFFLINE-ONLY — never reached from the request
    path (D-N1-4). The auto-refresh sync (N2) and the one-shot ``sync_mirror`` both build
    on this so neither re-implements the clone.

    Args:
        repo: The registry git URL (ignored when ``registry_root`` is given).
        registry_root: An existing local checkout to read instead of cloning.

    Returns:
        The parsed entries (malformed ``server.yaml`` files skipped + logged).

    Raises:
        FileNotFoundError: the checkout has no ``servers/`` directory.
        subprocess.CalledProcessError: the git clone failed.
    """
    if registry_root is not None:
        return build_mirror_entries(registry_root)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "mcp-registry"
        _clone_registry(repo, dest)
        return build_mirror_entries(dest)


def sync_mirror(
    *,
    repo: str = REGISTRY_REPO,
    mirror_path: Path,
    registry_root: Path | None = None,
) -> int:
    """Sync the mirror snapshot from the registry (OFFLINE-ONLY; D-N1-4).

    Builds the entries first and writes only on success, so a clone/parse failure
    leaves the last good snapshot intact (never a half-written file).

    Args:
        repo: The registry git URL (ignored when ``registry_root`` is given).
        mirror_path: Where to write ``mirror.json``.
        registry_root: An existing local checkout to read instead of cloning (used by
            tests + local runs to avoid the network).

    Returns:
        The number of servers written.

    Raises:
        FileNotFoundError: the checkout has no ``servers/`` directory.
        subprocess.CalledProcessError: the git clone failed.
    """
    entries = build_entries_from_source(repo=repo, registry_root=registry_root)
    write_mirror_atomic(entries, mirror_path)
    _log.info("mcp mirror synced", path=str(mirror_path), server_count=len(entries))
    return len(entries)
