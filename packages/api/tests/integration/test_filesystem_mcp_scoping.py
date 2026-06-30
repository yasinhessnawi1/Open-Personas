"""Integration tests for Spec P4 — per-(owner, persona) scoping of the builtin
``filesystem`` MCP subprocess (real spawns).

The bug is subprocess-specific: a stub/in-process test cannot exercise the
spawn-time scope hand-off that is the whole spec. These tests spawn the real
child via :class:`BuiltinMCPSupervisor`, thread a per-(owner, persona) scoped
root in at spawn, and prove:

- **Cross-context isolation** (P4 AC#1): owner A's child cannot read/write owner
  B's files; a single owner's two personas are isolated (community AC#3).
- **Child lifecycle** (P4-D-1): a DISTINCT child per scope key (not just distinct
  file visibility), and teardown actually reaps every child (the reaping path
  works, not just exists — the hinge under "proliferation is theoretical").
- **Fail closed** (P4-D-4 / AC#2): no scope ⇒ serve-and-deny end-to-end, never a
  shared-root fallback.

Marked ``integration`` (spawns subprocesses + binds loopback ports); run with
``pytest -m integration -k filesystem_mcp_scoping``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.tools.mcp.client import MCPClient
from persona_api.mcp import BuiltinMCPSupervisor

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_WRITE = "mcp:filesystem:write_file"
_READ = "mcp:filesystem:read_file"


async def _tool(client: MCPClient, name: str) -> object:
    return next(t for t in client.get_tools() if t.name == name)


async def _write(url: str, path: str, content: str) -> object:
    client = MCPClient(server_name="filesystem", server_url=url)
    await client.connect(strict=True)
    try:
        return await (await _tool(client, _WRITE)).execute(path=path, content=content)  # type: ignore[attr-defined]
    finally:
        await client.disconnect()


async def _read(url: str, path: str) -> object:
    client = MCPClient(server_name="filesystem", server_url=url)
    await client.connect(strict=True)
    try:
        return await (await _tool(client, _READ)).execute(path=path)  # type: ignore[attr-defined]
    finally:
        await client.disconnect()


async def test_filesystem_child_isolated_across_owners(tmp_path: Path) -> None:
    sup = BuiltinMCPSupervisor(("filesystem",))
    scope_a = tmp_path / "ownerA" / "personaX"
    scope_b = tmp_path / "ownerB" / "personaX"
    try:
        urls_a = await sup.resolve([_WRITE], filesystem_scope_root=scope_a)
        await _write(urls_a["filesystem"], "secret.txt", "A-only data")
        # Owner A's write lands under owner A's scoped root.
        assert (scope_a / "secret.txt").read_text() == "A-only data"

        # Owner B's child is rooted at owner B's scope — the same relative path
        # resolves to a DIFFERENT (empty) location: B cannot read A's file.
        urls_b = await sup.resolve([_READ], filesystem_scope_root=scope_b)
        result_b = await _read(urls_b["filesystem"], "secret.txt")
        assert result_b.is_error  # type: ignore[attr-defined]
        assert "A-only data" not in result_b.content  # type: ignore[attr-defined]
        assert not (scope_b / "secret.txt").exists()
    finally:
        await sup.aclose()


async def test_filesystem_single_owner_two_personas_isolated(tmp_path: Path) -> None:
    # Community single-owner posture (AC#3): persona-level scoping still isolates.
    sup = BuiltinMCPSupervisor(("filesystem",))
    scope_p1 = tmp_path / "ownerA" / "persona1"
    scope_p2 = tmp_path / "ownerA" / "persona2"
    try:
        urls_1 = await sup.resolve([_WRITE], filesystem_scope_root=scope_p1)
        await _write(urls_1["filesystem"], "p1.txt", "persona1 data")

        urls_2 = await sup.resolve([_READ], filesystem_scope_root=scope_p2)
        result = await _read(urls_2["filesystem"], "p1.txt")
        assert result.is_error  # type: ignore[attr-defined]
        assert not (scope_p2 / "p1.txt").exists()
    finally:
        await sup.aclose()


async def test_filesystem_distinct_child_per_scope_key_and_reaped(tmp_path: Path) -> None:
    # Lifecycle (P4-D-1): distinct child per scope key (not just file visibility),
    # and aclose() actually reaps every child.
    sup = BuiltinMCPSupervisor(("filesystem",))
    scope_a = tmp_path / "ownerA" / "personaX"
    scope_b = tmp_path / "ownerB" / "personaX"
    try:
        urls_a = await sup.resolve([_WRITE], filesystem_scope_root=scope_a)
        urls_b = await sup.resolve([_WRITE], filesystem_scope_root=scope_b)
        # Two distinct scopes ⇒ two distinct children (distinct loopback URLs).
        assert urls_a["filesystem"] != urls_b["filesystem"]
        assert sup.running_server_count == 2
        # Re-resolving the SAME scope is idempotent — no third process.
        urls_a2 = await sup.resolve([_WRITE], filesystem_scope_root=scope_a)
        assert urls_a2["filesystem"] == urls_a["filesystem"]
        assert sup.running_server_count == 2
    finally:
        await sup.aclose()
    # Teardown reaped every scoped child (the reaping path works, not just exists).
    assert sup.running_server_count == 0


async def test_filesystem_no_scope_fails_closed_end_to_end() -> None:
    # P4-D-4 / AC#2 defense-in-depth: a child spawned WITHOUT a scope serves but
    # denies every call — never falls back to the shared root.
    sup = BuiltinMCPSupervisor(("filesystem",))
    try:
        urls = await sup.resolve([_WRITE], filesystem_scope_root=None)
        # The tool is reachable (serve-and-deny, not silently absent)…
        result = await _write(urls["filesystem"], "leak.txt", "x")
        # …but every write is denied.
        assert result.is_error  # type: ignore[attr-defined]
    finally:
        await sup.aclose()
