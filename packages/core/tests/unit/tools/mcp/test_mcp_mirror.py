"""Unit tests for the Docker MCP catalog mirror (Spec N1 T2, D-N1-3 / D-N1-4).

Two halves, structurally separated:

- the **request-path loader** (``persona.tools.mcp.mirror``) — zero-network, must
  NEVER raise at boot; missing / corrupt / invalid snapshot falls back to the
  bundled ``catalog.toml`` builtin catalog;
- the **offline sync** (``persona.tools.mcp.mirror_sync``) — git clone → parse
  ``server.yaml`` → validate → atomic write; one bad ``server.yaml`` is skipped +
  logged, never fatal; a sync failure leaves the last good snapshot intact.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

from persona.tools.mcp.catalog import BUILTIN_MCP_CATALOG
from persona.tools.mcp.mirror import (
    MIRROR_PATH,
    load_mirror_catalog,
    resolve_mirror_write_path,
)
from persona.tools.mcp.mirror_sync import (
    build_mirror_entries,
    parse_server_yaml,
    sync_mirror,
    write_mirror_atomic,
)

if TYPE_CHECKING:
    from pathlib import Path

# A github-official-shaped server.yaml (the real registry schema, trimmed).
_GOOD_SERVER_YAML = textwrap.dedent(
    """
    name: github-official
    image: ghcr.io/github/github-mcp-server
    type: server
    meta:
      category: devops
      tags: [github, devops]
    about:
      title: GitHub Official
      description: Official GitHub MCP Server.
      icon: https://avatars.githubusercontent.com/u/9919?s=200&v=4
    source:
      project: https://github.com/github/github-mcp-server
      commit: 23fa0dd1a821d1346c1de2abafe7327d26981606
    run:
      allowHosts:
        - api.github.com:443
        - github.com:443
    config:
      secrets:
        - name: github.personal_access_token
          env: GITHUB_PERSONAL_ACCESS_TOKEN
          example: <YOUR_TOKEN>
          description: Create a token on GitHub.
    """
)


def _write_server(registry_root: Path, name: str, body: str) -> None:
    """Write ``servers/<name>/server.yaml`` under a fake registry checkout."""
    d = registry_root / "servers" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "server.yaml").write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------- #
# parse_server_yaml — the server.yaml → MCPServerCatalogEntry mapping          #
# --------------------------------------------------------------------------- #


def test_parse_server_yaml_maps_docker_display_metadata() -> None:
    import yaml

    data = yaml.safe_load(_GOOD_SERVER_YAML)
    entry = parse_server_yaml("github-official", data)
    assert entry is not None
    assert entry.name == "github-official"
    assert entry.kind == "external"  # Persona ships no code for mirror servers
    assert entry.server_type == "server"
    assert entry.display_name == "GitHub Official"
    assert entry.description == "Official GitHub MCP Server."
    assert entry.image == "ghcr.io/github/github-mcp-server"
    assert entry.source_commit == "23fa0dd1a821d1346c1de2abafe7327d26981606"
    assert entry.allow_hosts == ("api.github.com:443", "github.com:443")
    assert len(entry.secrets) == 1
    assert entry.secrets[0].env == "GITHUB_PERSONAL_ACCESS_TOKEN"
    # A secret-requiring server is risk-labelled medium (touches an external account).
    assert entry.risk == "medium"


def test_parse_server_yaml_captures_remote_url_for_remote_servers() -> None:
    # Spec N4: a type:remote server's hosted endpoint (remote.url) is the catalog's
    # trust anchor for WHERE an adopted app connects (N4-D-X-catalog-remote-url).
    data = {
        "type": "remote",
        "about": {"title": "Notion (Remote)", "description": "Hosted Notion MCP."},
        "remote": {"url": "https://mcp.notion.com/mcp", "transport_type": "streamable-http"},
    }
    entry = parse_server_yaml("notion-remote", data)
    assert entry is not None
    assert entry.server_type == "remote"
    assert entry.remote_url == "https://mcp.notion.com/mcp"


def test_parse_server_yaml_local_server_has_empty_remote_url() -> None:
    # A local-container (type:server) entry has no remote endpoint → remote_url stays ""
    # → not v1-adoptable (consistent with N4-D-2's local-container deferral).
    data = {"type": "server", "about": {"title": "Local"}, "image": "ghcr.io/x/y"}
    entry = parse_server_yaml("local-x", data)
    assert entry is not None
    assert entry.server_type == "server"
    assert entry.remote_url == ""


def test_parse_server_yaml_skips_malformed_structure() -> None:
    # `about` as a scalar (not a mapping) is malformed → skip (return None), never raise.
    assert parse_server_yaml("broken", {"about": "not-a-mapping"}) is None


# --------------------------------------------------------------------------- #
# build_mirror_entries — walk the checkout, skip bad ones                      #
# --------------------------------------------------------------------------- #


def test_build_skips_one_bad_server_yaml_without_failing_the_sync(tmp_path: Path) -> None:
    _write_server(tmp_path, "github-official", _GOOD_SERVER_YAML)
    _write_server(tmp_path, "garbled", ":::not: valid: yaml: [")  # unparseable
    _write_server(tmp_path, "structurally-bad", "about: 12345\n")  # parses, bad shape
    entries = build_mirror_entries(tmp_path)
    names = {e.name for e in entries}
    assert names == {"github-official"}  # the two bad ones skipped, the good one kept


def test_build_raises_when_no_servers_dir(tmp_path: Path) -> None:
    # A clone that produced nothing (no servers/ dir) → the sync fails loudly BEFORE
    # any write, so the last good snapshot is preserved (see the atomicity test).
    import pytest

    with pytest.raises(FileNotFoundError):
        build_mirror_entries(tmp_path)


# --------------------------------------------------------------------------- #
# write_mirror_atomic + load_mirror_catalog — round-trip                       #
# --------------------------------------------------------------------------- #


def test_write_then_load_roundtrips_and_leaves_no_temp_file(tmp_path: Path) -> None:
    _write_server(tmp_path, "github-official", _GOOD_SERVER_YAML)
    entries = build_mirror_entries(tmp_path)
    mirror = tmp_path / "mirror.json"
    write_mirror_atomic(entries, mirror)

    catalog = load_mirror_catalog(mirror)
    assert set(catalog.servers) == {"github-official"}
    assert catalog.servers["github-official"].display_name == "GitHub Official"
    assert catalog.servers["github-official"].secrets[0].env == "GITHUB_PERSONAL_ACCESS_TOKEN"
    # No half-written temp file lingering in the dir.
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


# --------------------------------------------------------------------------- #
# load_mirror_catalog — the fail-soft fallback chain (NEVER raises at boot)    #
# --------------------------------------------------------------------------- #


def test_missing_snapshot_falls_back_to_builtin(tmp_path: Path) -> None:
    catalog = load_mirror_catalog(tmp_path / "does-not-exist.json")
    assert catalog is BUILTIN_MCP_CATALOG  # the bundled catalog.toml, unchanged


def test_corrupt_snapshot_falls_back_to_builtin(tmp_path: Path) -> None:
    # Explicit corrupt case (not just missing): invalid JSON must NOT raise.
    bad = tmp_path / "mirror.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    catalog = load_mirror_catalog(bad)
    assert catalog is BUILTIN_MCP_CATALOG


def test_structurally_invalid_snapshot_falls_back_to_builtin(tmp_path: Path) -> None:
    # Valid JSON, but a server entry violates the model (bad risk literal) → fallback.
    bad = tmp_path / "mirror.json"
    bad.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": [
                    {"name": "x", "description": "d", "kind": "external", "risk": "nonsense"}
                ],
            }
        ),
        encoding="utf-8",
    )
    catalog = load_mirror_catalog(bad)
    assert catalog is BUILTIN_MCP_CATALOG


# --------------------------------------------------------------------------- #
# sync_mirror — a failure mid-sync preserves the existing snapshot             #
# --------------------------------------------------------------------------- #


def test_sync_failure_preserves_the_last_good_mirror(tmp_path: Path) -> None:
    # Seed an existing good snapshot.
    good_registry = tmp_path / "registry"
    _write_server(good_registry, "github-official", _GOOD_SERVER_YAML)
    mirror = tmp_path / "mirror.json"
    write_mirror_atomic(build_mirror_entries(good_registry), mirror)
    before = mirror.read_bytes()

    # A sync against a checkout with no servers/ dir raises BEFORE writing.
    import pytest

    empty_registry = tmp_path / "empty"
    empty_registry.mkdir()
    with pytest.raises(FileNotFoundError):
        sync_mirror(registry_root=empty_registry, mirror_path=mirror)

    assert mirror.read_bytes() == before  # last good snapshot intact, not truncated


def test_sync_from_local_checkout_writes_snapshot(tmp_path: Path) -> None:
    # registry_root injection avoids any network/git in the unit test.
    registry = tmp_path / "registry"
    _write_server(registry, "github-official", _GOOD_SERVER_YAML)
    _write_server(registry, "garbled", ":::bad")
    mirror = tmp_path / "mirror.json"
    count = sync_mirror(registry_root=registry, mirror_path=mirror)
    assert count == 1  # garbled skipped
    assert load_mirror_catalog(mirror).servers.keys() == {"github-official"}


# --------------------------------------------------------------------------- #
# load_mirror_catalog override precedence (N2-D-1: override → bundled → builtin)
# --------------------------------------------------------------------------- #


def test_override_snapshot_takes_precedence_over_bundled(tmp_path: Path) -> None:
    # A valid override is preferred over the bundled `path` snapshot.
    registry = tmp_path / "registry"
    _write_server(registry, "github-official", _GOOD_SERVER_YAML)
    override = tmp_path / "override" / "mirror.json"
    sync_mirror(registry_root=registry, mirror_path=override)
    bundled = tmp_path / "bundled.json"  # absent → would fall back to builtin if used

    catalog = load_mirror_catalog(bundled, override=override)
    assert set(catalog.servers) == {"github-official"}


def test_corrupt_override_falls_through_to_bundled_then_builtin(tmp_path: Path) -> None:
    # A corrupt override must NOT skip straight to builtin: it tries the bundled snapshot
    # next, and only then the builtin fallback.
    bad_override = tmp_path / "override.json"
    bad_override.write_text("{ not json", encoding="utf-8")

    registry = tmp_path / "registry"
    _write_server(registry, "github-official", _GOOD_SERVER_YAML)
    bundled = tmp_path / "bundled.json"
    sync_mirror(registry_root=registry, mirror_path=bundled)

    catalog = load_mirror_catalog(bundled, override=bad_override)
    assert set(catalog.servers) == {"github-official"}  # the bundled snapshot, not builtin

    # Both unusable → builtin.
    bad_bundled = tmp_path / "bad_bundled.json"
    bad_bundled.write_text("{ also not json", encoding="utf-8")
    assert load_mirror_catalog(bad_bundled, override=bad_override) is BUILTIN_MCP_CATALOG


def test_unset_override_uses_bundled_path(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    _write_server(registry, "github-official", _GOOD_SERVER_YAML)
    bundled = tmp_path / "mirror.json"
    sync_mirror(registry_root=registry, mirror_path=bundled)
    catalog = load_mirror_catalog(bundled, override=None)
    assert set(catalog.servers) == {"github-official"}


def test_resolve_mirror_write_path_prefers_override(tmp_path: Path) -> None:
    override = tmp_path / "vol" / "mirror.json"
    assert resolve_mirror_write_path(override) == override


def test_resolve_mirror_write_path_never_falls_back_to_bundled_file() -> None:
    """R9-011 regression pin: with no override, the WRITE path is ``None`` — never the
    bundled package-data ``MIRROR_PATH`` (a sync would rewrite a committed file)."""
    resolved = resolve_mirror_write_path(None)
    assert resolved is None
    assert resolved != MIRROR_PATH
