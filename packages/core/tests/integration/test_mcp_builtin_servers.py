"""End-to-end integration tests for the built-in MCP servers (Spec 27 T6-T9).

Each test spawns a real built-in server via its ``python -m
persona.tools.mcp.builtin`` entrypoint over real Streamable HTTP and drives it
through the in-tree :class:`~persona.tools.mcp.client.MCPClient` — proving the
harness + entrypoint + transport + delegation all work together, not in a mock.

Marked ``integration`` (spawns a subprocess + binds a loopback port); run with
``pytest -m integration -k mcp_builtin``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.integration._mcp_spawn import connected_builtin

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_time_server_dispatches_datetime_now() -> None:
    async with connected_builtin("time") as client:
        tools = {t.name: t for t in client.get_tools()}
        assert "mcp:time:datetime" in tools
        result = await tools["mcp:time:datetime"].execute(operation="now", timezone="UTC")
        assert not result.is_error
        assert "UTC" in result.content


async def test_calculator_server_dispatches_calculate() -> None:
    async with connected_builtin("calculator") as client:
        tools = {t.name: t for t in client.get_tools()}
        assert "mcp:calculator:calculate" in tools
        result = await tools["mcp:calculator:calculate"].execute(expression="2 ** 10")
        assert not result.is_error
        assert "1024" in result.content


async def test_filesystem_server_write_read_and_rejects_escape(tmp_path: Path) -> None:
    # Spec P4: the server roots at the per-spawn scoped root threaded in over
    # PERSONA_FILESYSTEM_SCOPE_ROOT (not the process-wide PERSONA_TOOLS_SANDBOX_ROOT).
    env = {"PERSONA_FILESYSTEM_SCOPE_ROOT": str(tmp_path)}
    async with connected_builtin("filesystem", extra_env=env) as client:
        tools = {t.name: t for t in client.get_tools()}
        assert {"mcp:filesystem:read_file", "mcp:filesystem:write_file"} <= set(tools)

        ok = await tools["mcp:filesystem:write_file"].execute(path="notes/a.txt", content="inside")
        assert not ok.is_error
        assert (tmp_path / "notes" / "a.txt").read_text() == "inside"

        # A '..' escape must fail gracefully (is_error) and write nothing outside.
        escape = await tools["mcp:filesystem:write_file"].execute(
            path="../escaped.txt", content="nope"
        )
        assert escape.is_error
        assert not (tmp_path.parent / "escaped.txt").exists()


async def test_weather_server_advertises_get_weather() -> None:
    # Spawn-only (no real network): prove the opt-in server boots + advertises.
    async with connected_builtin("weather") as client:
        assert "mcp:weather:get_weather" in {t.name for t in client.get_tools()}
