"""Shared parsers for ``mcp:`` allow-list entry names (Spec N7, D-N7-1).

A persona's ``tools`` allow-list carries two shapes of MCP entry:

- the bare **server-grant** form ``mcp:<server>`` — what the apps UX toggle
  writes (N3): the user enables an APP, and composition expands the grant into
  that server's real tool names at toolbox build (D-N7-1);
- the fully-qualified **tool** form ``mcp:<server>:<tool>`` — the name every
  MCP-discovered tool actually registers under (D-03-19 adapter naming; the
  Toolbox allow-list stays literal exact-match, no prefix magic in the hot path).

Three seams parse these shapes and MUST agree: the toolbox-build grant expansion
(``persona.tools._factory``), the server-enablement predicate on the catalog/API
side (``persona_api.services.catalog_service``), and the builtin-launcher's
needed-servers scan (``persona_api.mcp.builtin_launcher``). The launcher scan
deliberately has WIDER semantics — a persona declaring only tool-level names
still needs the server spawned — hence two functions, not one. The web mirrors
the server-grant contract in TypeScript (``app-state.ts`` ``isAppEnabled``:
exactly one colon).
"""

from __future__ import annotations

__all__ = ["referenced_server_name", "server_grant_name"]

#: The allow-list prefix every MCP entry carries (grant and tool forms alike).
_MCP_PREFIX = "mcp:"


def server_grant_name(entry: str) -> str | None:
    """The server name iff ``entry`` is the bare server-grant form ``mcp:<server>``.

    The grant form has EXACTLY one colon and a non-empty server name
    (e.g. ``mcp:github``). Tool-level entries (``mcp:<server>:<tool>``) and
    non-MCP entries are NOT server grants.

    Args:
        entry: One persona ``tools`` allow-list entry.

    Returns:
        The granted server name, or ``None`` when ``entry`` is not a server grant.
    """
    if not entry.startswith(_MCP_PREFIX):
        return None
    rest = entry[len(_MCP_PREFIX) :]
    if not rest or ":" in rest:
        return None
    return rest


def referenced_server_name(entry: str) -> str | None:
    """The server name ANY ``mcp:`` entry references (grant OR tool form).

    Wider than :func:`server_grant_name`: both ``mcp:<server>`` and
    ``mcp:<server>:<tool>`` reference ``<server>`` — the builtin-launcher scan
    semantics (a persona declaring only tool-level names still needs the server
    running).

    Args:
        entry: One persona ``tools`` allow-list entry.

    Returns:
        The referenced server name, or ``None`` for non-MCP entries and entries
        with an empty server segment (``"mcp:"`` / ``"mcp::…"``).
    """
    if not entry.startswith(_MCP_PREFIX):
        return None
    name = entry[len(_MCP_PREFIX) :].split(":", 1)[0]
    return name or None
