"""Tool + skill catalog (spec 08, T13, §5.4).

Read-only platform-global lists of the available tools and bundled skills, for
the web app's authoring-flow checkboxes. Not RLS-scoped (no tenant data).

Tools: sourced from the persona-core known-tool catalog (spec 26 T08,
``persona.tools.TOOL_CATALOG``) — the single source of truth for every built-in
platform tool, including runtime-wired ones (``code_execution`` /
``generate_image`` / ``text_summarize``) whose factories need runtime context.
The runtime fails loud (D-12-5, D-15-X) if a persona declares a runtime-wired
tool that is not configured on the deployment.

Skills: the bundled v0.1 skill set scanned from ``persona/skills/builtin``
(architecture §9.3 + spec 13).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.config import PersonaCoreConfig
from persona.schema.skills import SkillSpec
from persona.skills import BUILTIN_ROOT, SkillScanner
from persona.skills.skill_mirror import load_skill_mirror, resolve_skill_mirror_write_path
from persona.tools import TOOL_CATALOG
from persona.tools.mcp.catalog import BUILTIN_MCP_CATALOG, MCPServerCatalogEntry
from persona.tools.mcp.mirror import load_mirror_catalog

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "available_mcp_server_names",
    "list_skills",
    "list_specialities",
    "list_tools",
    "merged_mcp_catalog",
    "unavailable_enabled_mcp_servers",
]

# Every bundled skill folder under persona/skills/builtin (architecture §9.3,
# spec 13). The scanner emits one entry per declared skill that exists on disk.
# Spec 24 (D-24-1): the 5 document-format packs folded into document_generation.
_BUILTIN_SKILLS = [
    "code_review",
    "data_analysis",
    "document_generation",
    "web_research",
]


def list_tools() -> list[tuple[str, str]]:
    """The built-in tools as (name, description) pairs (spec 26 T08).

    Sourced from the persona-core known-tool catalog so the authoring surface
    always reflects the full tool set — including the spec-26 additions
    (calculator / datetime / regex_match / json_query / text_diff /
    currency_convert / text_summarize) — without a second hand-maintained list.
    """
    return [(entry.name, entry.description) for entry in TOOL_CATALOG]


def list_skills() -> list[tuple[str, str]]:
    """The bundled skills as (name, description) pairs."""
    scanner = SkillScanner(skill_paths=[BUILTIN_ROOT])
    scanned = scanner.scan(declared_skills=_BUILTIN_SKILLS)
    return [(s.name, s.description) for s in scanned]


def list_specialities(*, skill_mirror_path: Path | None = None) -> list[SkillSpec]:
    """The full speciality catalog: bundled builtins + S2's synced external skills.

    Each :class:`SkillSpec` carries its **source-assigned** trust tier (S1-D-3) and
    ``provenance.content_hash`` (S1-D-5) — the data the user-facing Specialities surface
    (S3) renders and binds consent to. Builtin skills are scanned from the repo (assigned
    ``builtin``); external skills come from the S2 mirror snapshot (``vetted`` / ``community``
    / ``third_party`` per source). The mirror is fail-soft: no sync yet → just the builtins.

    Builtin wins on a name collision (the ``merged_mcp_catalog`` / ``declared_mirror_skills``
    precedent — a repo skill is never superseded by an external one of the same name); the
    external tail is unioned in, deterministic order (builtins first).

    Args:
        skill_mirror_path: Override the mirror snapshot path (tests). ``None`` → the
            configured writable volume override, else the bundled default.
    """
    scanner = SkillScanner(skill_paths=[BUILTIN_ROOT])
    builtin = scanner.scan(declared_skills=_BUILTIN_SKILLS)
    mirror_path = (
        skill_mirror_path
        if skill_mirror_path is not None
        else resolve_skill_mirror_write_path(PersonaCoreConfig().skill_mirror_path)
    )
    external = load_skill_mirror(mirror_path)
    seen = {s.name for s in builtin}
    return list(builtin) + [s for s in external if s.name not in seen]


def merged_mcp_catalog(*, mirror_path: Path | None = None) -> list[MCPServerCatalogEntry]:
    """The MCP catalog the management UI lists: builtin floor + Docker mirror (N1).

    Spec 30 T11 + N1 (D-N1-3/4). The bundled ``catalog.toml`` is the authoritative
    **floor** — it is also the fail-soft fallback target — so its authored entries
    (curated security notes, gap-detection keywords, verb-phrase capabilities)
    **supersede a same-named mirror entry**; the ~220–300 Docker-registry mirror fills
    the long tail. Merge policy is therefore a name-keyed union with **builtin-wins on
    collision**, in a deterministic order: builtins (``catalog.toml`` order) first,
    then mirror entries (mirror order) whose names are not already a builtin.

    When no mirror snapshot exists, :func:`load_mirror_catalog` returns the builtin
    catalog (its fallback), so the result is exactly the builtins — a turn with no
    mirror still resolves every built-in server. A persona enables a server by adding
    ``mcp:<name>`` to its ``tools`` allow-list; bring-your-own servers are NOT here
    (they live per-user, surfaced via ``GET /v1/mcp-servers``).

    N2 (N2-D-1): the mirror half prefers the auto-synced ``override`` snapshot
    (``PERSONA_MCP_MIRROR_PATH`` — the writable mirror on the mounted volume) when configured,
    falling through to the bundled snapshot then the builtin floor. ``mirror_path`` defaults to
    the env-derived override; tests pass it explicitly. A server the auto-sync *removed* is no
    longer in the mirror, so it is not listed here as enableable (N2-D-4 surface a).
    """
    override = mirror_path if mirror_path is not None else PersonaCoreConfig().mcp_mirror_path
    merged: dict[str, MCPServerCatalogEntry] = dict(BUILTIN_MCP_CATALOG.servers)
    for name, entry in load_mirror_catalog(override=override).servers.items():
        if name not in merged:  # builtin-wins on name collision
            merged[name] = entry
    return list(merged.values())


def available_mcp_server_names(*, mirror_path: Path | None = None) -> set[str]:
    """The set of MCP server names currently AVAILABLE to enable (builtin + mirror).

    The N2 availability surface: the builtin floor plus whatever the auto-synced mirror
    currently lists. A server the auto-sync removed is absent here — so it is neither
    offered as newly-enableable (N2-D-4 surface a) nor counted as available for an
    already-enabled persona's flag (surface c).
    """
    return {e.name for e in merged_mcp_catalog(mirror_path=mirror_path)}


def _is_mcp_server_enablement(entry: str) -> bool:
    """Whether a ``tools`` allow-list entry enables a CATALOG SERVER (``mcp:<name>``).

    A persona enables a catalog server by carrying ``mcp:<name>`` — exactly one colon.
    Deeper-prefixed entries are tool-level, not server-enablement: the Docker-gateway tools
    ``mcp:docker:<tool>`` (D-N1-6) and any ``mcp:<server>:<tool>`` refinement are excluded
    (a removed *tool* on a live server is the adapter's §7.3 graceful path, not this signal).
    """
    return entry.startswith("mcp:") and entry.count(":") == 1 and len(entry) > len("mcp:")


def unavailable_enabled_mcp_servers(
    tools: Sequence[str], *, mirror_path: Path | None = None
) -> list[str]:
    """Which of a persona's enabled MCP servers are no longer available (N2-D-4 surface c).

    The owner-visible signal: given a persona's ``tools`` allow-list, return the names of the
    ``mcp:<name>`` catalog-server enablements whose server is no longer in the availability set
    (e.g. the auto-sync removed it upstream), in allow-list order, de-duplicated. An empty
    result (the common case — nothing enabled, or all still available) does NOT touch the
    mirror file (the catalog is only loaded when there is at least one enablement to check).

    This is the graceful *flag*: the live tool-call path already degrades without crashing
    (the adapter returns an error ``ToolResult`` on a dead connection, §7.3; the Toolbox simply
    never advertises a tool whose server is gone), so this surfaces the disappearance to the
    owner rather than letting an enabled capability vanish silently.
    """
    enabled = [entry.split(":", 1)[1] for entry in tools if _is_mcp_server_enablement(entry)]
    if not enabled:
        return []
    available = available_mcp_server_names(mirror_path=mirror_path)
    unavailable: list[str] = []
    seen: set[str] = set()
    for name in enabled:
        if name not in available and name not in seen:
            seen.add(name)
            unavailable.append(name)
    return unavailable
