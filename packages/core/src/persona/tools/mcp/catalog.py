"""Declarative built-in MCP server catalog (Spec 27, D-27-1 / D-27-10).

Loads the bundled ``catalog.toml`` — a declarative index of the MCP servers
Persona knows about (authored built-ins + bring-your-own external servers). This
is the **precursor** to the deferred federated MCP registry (architecture §9.4),
not the registry: resolution is 100% local, zero-network, exactly like the
Spec-24 skills catalog. ``tomllib`` (stdlib) parses the file — no new dependency.

The catalog is the shared vocabulary for:

- the **built-in launcher** (Spec 27 T4) — which servers may be spawned, and the
  default-enabled subset (``default_enabled`` ∩ ``kind="builtin"``);
- the **capability recommender** (Spec 27 T10) — MCP candidates ranked alongside
  built-in tools + skills, each carrying a provider tag;
- the runtime **MCP-gap detector** (Spec 27 T11) — maps a capability-gap utterance
  to a catalog server the persona's allow-list lacks (``keywords``, D-27-7).

Entries are frozen + ``extra="forbid"`` so a malformed catalog fails loud at
import (fail-fast). The catalog never validates a persona's declared ``mcp:``
names — unknown/external servers resolve from ``PERSONA_MCP_SERVERS`` (D-03-22).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "BUILTIN_MCP_CATALOG",
    "CATALOG_PATH",
    "MCPCatalog",
    "MCPSecretField",
    "MCPServerCatalogEntry",
    "authored_server_names",
    "default_enabled_server_names",
    "known_mcp_server_names",
    "load_mcp_catalog",
    "mcp_server_entry",
    "recommender_provider_tag",
]

#: Bundled catalog file, shipped as package data alongside the MCP client/adapter.
CATALOG_PATH: Path = Path(__file__).parent / "catalog.toml"

MCPServerKind = Literal["builtin", "external"]
MCPRiskLevel = Literal["low", "medium", "high"]
#: Docker catalog taxonomy (N1, D-N1-3): a Docker-registry server is ``"server"``
#: (a local-run container image) or ``"remote"`` (a hosted endpoint). Builtin /
#: bring-your-own catalog rows that predate the mirror default to ``"builtin"`` /
#: ``"external"`` mirroring their ``kind`` — the field is consumed only for mirror
#: entries; ``kind`` remains the authority for the recommender/launcher.
MCPServerType = Literal["server", "remote", "builtin", "external"]


class MCPSecretField(BaseModel):
    """One credential a Docker MCP server declares (N1, D-N1-3 / D-N1-5).

    Mapped from a ``server.yaml`` ``config.secrets[]`` entry. This is **display-only
    metadata** — the credential *schema* the apps UX (N3) renders so a user knows
    *which* secret to provide. It deliberately carries **no value field**: the
    catalog can never transport a credential, even by accident. The actual storage
    + passing of a secret is out of N1's connect-only live path (D-N1-5); per-user
    injection is the N4 foundation.

    Attributes:
        name: The gateway's secret key (e.g. ``github.personal_access_token``) — the
            key the Docker Gateway injects the value under at runtime.
        env: The environment variable the server reads the secret from (e.g.
            ``GITHUB_PERSONAL_ACCESS_TOKEN``).
        example: An authoring-facing placeholder (e.g. ``<YOUR_TOKEN>``). Never a
            real value.
        description: Human guidance for obtaining the credential (rendered by N3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    env: str
    example: str = ""
    description: str = ""


class MCPServerCatalogEntry(BaseModel):
    """One MCP server's catalog metadata (frozen).

    Attributes:
        name: The server identifier — the ``mcp:<name>:`` prefix on its tools and
            the key in ``PERSONA_MCP_SERVERS`` / ``PERSONA_MCP_BUILTIN_ENABLED``.
        description: Authoring-facing summary (distinct from the model-facing tool
            descriptions the server advertises at connect time).
        kind: ``"builtin"`` — Persona authors + can launch this server (D-27-2);
            ``"external"`` — bring-your-own, operator-configured via
            ``PERSONA_MCP_SERVERS`` (Persona ships no code).
        default_enabled: Whether a default install enables this server. Applies to
            ``kind="builtin"`` only; with lazy spawning (D-27-3) "enabled" means
            "registered + available", not "running".
        risk: Coarse risk profile for operator review.
        required_env: Env vars the operator must set for the server to function
            (e.g. ``GITHUB_TOKEN`` for github). Never logged.
        keywords: Capability phrases that map a "I can't do X" utterance to this
            server (Spec 27 T11 gap-detection vocabulary, D-27-7). Lower-cased
            substrings matched against the model's output; empty disables mapping.
        capability: A first-person VERB phrase for the runtime MCP-gap consent
            line — slots into ``"…which would let me {capability}."`` (Spec 27
            T11). Distinct from ``description`` (a noun phrase tuned for authoring
            / the recommender list); a verb phrase keeps the consent question
            grammatical. Empty falls back to ``description``.
        notes: Optional operator-facing caveats (e.g. the fetch SSRF rationale).

    N1 (D-N1-3) display-metadata fields — populated for **Docker catalog-mirror**
    entries (from ``docker/mcp-registry`` ``server.yaml``), rendered by the apps UX
    (N3). EVERY one is additive-with-default so the bundled ``catalog.toml`` (which
    declares none of them) still validates against this frozen, ``extra="forbid"``
    model — a non-defaulted field here would break the catalog load at import.

        display_name: Friendly title (``about.title``); ``""`` falls back to ``name``.
        icon_url: Server icon (``about.icon``) for the apps list.
        image: Container image reference (``image``), e.g. ``ghcr.io/...``.
        server_type: Docker taxonomy (``type``) — ``"server"`` / ``"remote"`` for
            mirror entries; defaults to ``"builtin"`` for the bundled rows.
        remote_url: The hosted endpoint URL for a ``type="remote"`` server (from
            ``server.yaml`` ``remote.url``) — the catalog's **trust anchor for where an
            adopted app connects** (Spec N4, N4-D-X-catalog-remote-url). Empty for
            local-container / builtin entries (no remote endpoint) → not v1-adoptable
            (consistent with N4-D-2's local-container deferral). Never a secret.
        source_project: Upstream repo URL (``source.project``) — provenance.
        source_commit: 40-char commit pin (``source.commit``) — reproducibility /
            provenance trust labeling.
        signed: Whether the image is signed (Docker ``--verify-signatures`` posture)
            — a trust label the UI surfaces.
        allow_hosts: The server's egress allowlist (``run.allowHosts``) — informational.
        secrets: The credential schema (``config.secrets[]``) — display-only
            (:class:`MCPSecretField`); never a value (D-N1-5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    kind: MCPServerKind
    risk: MCPRiskLevel
    default_enabled: bool = False
    required_env: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    capability: str = ""
    notes: str = ""
    # -- N1 (D-N1-3): Docker catalog-mirror display metadata (additive-with-default) --
    display_name: str = ""
    icon_url: str = ""
    image: str = ""
    server_type: MCPServerType = "builtin"
    remote_url: str = ""
    source_project: str = ""
    source_commit: str = ""
    signed: bool = False
    allow_hosts: tuple[str, ...] = ()
    secrets: tuple[MCPSecretField, ...] = ()
    # -- Spec R8 (R8-D-7): per-user OAuth binding (additive-with-default) --
    # ``auth_method = "oauth"`` marks a server whose credential is obtained per-user via
    # the OAuth dance (Connect flow), NOT an operator-global env token. ``oauth_provider``
    # is the provider-registry key (e.g. ``github``). Empty = the pre-R8 env/none path.
    # GitHub is rebound onto this (its ``required_env`` GITHUB_TOKEN bypass removed).
    auth_method: str = ""
    oauth_provider: str = ""


@dataclass(frozen=True)
class MCPCatalog:
    """Parsed ``catalog.toml`` — MCP servers keyed by name.

    Attributes:
        servers: Mapping of server name → :class:`MCPServerCatalogEntry`, in file
            order (``dict`` preserves insertion order).
    """

    servers: dict[str, MCPServerCatalogEntry]


def load_mcp_catalog(path: Path = CATALOG_PATH) -> MCPCatalog:
    """Parse a ``catalog.toml`` into an :class:`MCPCatalog`.

    Args:
        path: The catalog file (defaults to the bundled one).

    Returns:
        The parsed catalog.

    Raises:
        pydantic.ValidationError: an entry is malformed (fail-fast at import).
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    servers: dict[str, MCPServerCatalogEntry] = {}
    for name, entry in data.get("server", {}).items():
        servers[name] = MCPServerCatalogEntry.model_validate({"name": name, **entry})
    return MCPCatalog(servers=servers)


#: The bundled catalog, loaded once. Read-only configuration; never mutated.
BUILTIN_MCP_CATALOG: MCPCatalog = load_mcp_catalog()


def mcp_server_entry(name: str) -> MCPServerCatalogEntry | None:
    """Return the catalog entry for ``name``, or ``None`` if unknown."""
    return BUILTIN_MCP_CATALOG.servers.get(name)


def known_mcp_server_names() -> frozenset[str]:
    """The set of all catalog server names (builtin + external)."""
    return frozenset(BUILTIN_MCP_CATALOG.servers)


def authored_server_names() -> frozenset[str]:
    """The set of server names Persona authors + can launch (``kind="builtin"``)."""
    return frozenset(name for name, e in BUILTIN_MCP_CATALOG.servers.items() if e.kind == "builtin")


def default_enabled_server_names() -> tuple[str, ...]:
    """The authored servers a default install enables, in catalog order (D-27-4)."""
    return tuple(
        name
        for name, e in BUILTIN_MCP_CATALOG.servers.items()
        if e.kind == "builtin" and e.default_enabled
    )


def recommender_provider_tag(entry: MCPServerCatalogEntry) -> str:
    """Provider tag for the unified recommender (Spec 27 T10, spec §2.3).

    ``"mcp:builtin"`` for an authored, default-enabled server (available without
    operator action); ``"mcp:optional"`` otherwise (opt-in built-in or BYO
    external — both require an operator step before the persona can use them).
    """
    if entry.kind == "builtin" and entry.default_enabled:
        return "mcp:builtin"
    return "mcp:optional"
