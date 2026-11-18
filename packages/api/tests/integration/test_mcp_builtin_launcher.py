"""Integration tests for the built-in MCP supervisor (Spec 27 T4) — real spawns.

Proves the lazy per-server lifecycle against real subprocesses: spawn-on-resolve,
loopback bind, "math persona = 1 process", restart-on-death, and shutdown reaping.

Marked ``integration`` (spawns subprocesses + binds loopback ports); run with
``pytest -m integration -k mcp_builtin_launcher``.
"""

from __future__ import annotations

import pytest
from persona.tools.mcp.client import MCPClient
from persona_api.mcp import BuiltinMCPSupervisor

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_ensure_spawns_one_server_and_returns_loopback_url() -> None:
    sup = BuiltinMCPSupervisor(("time", "calculator", "filesystem"))
    try:
        url = await sup.ensure("time")
        assert url is not None
        assert url.startswith("http://127.0.0.1:")  # D-27-12: loopback only
        assert sup.running_server_count == 1
        # A second ensure is idempotent — no second process.
        assert await sup.ensure("time") == url
        assert sup.running_server_count == 1
    finally:
        await sup.aclose()
    assert sup.running_server_count == 0


async def test_math_persona_spawns_exactly_one_process() -> None:
    # The lazy-spawn headline: a persona that only does math boots 1 server,
    # not the whole default-enabled set (D-27-3).
    sup = BuiltinMCPSupervisor(("time", "calculator", "filesystem"))
    try:
        urls = await sup.resolve(["web_search", "mcp:calculator:calculate"])
        assert set(urls) == {"calculator"}
        assert sup.running_server_count == 1
    finally:
        await sup.aclose()


async def test_resolved_server_is_actually_callable() -> None:
    sup = BuiltinMCPSupervisor(("calculator",))
    try:
        urls = await sup.resolve(["mcp:calculator:calculate"])
        client = MCPClient(server_name="calculator", server_url=urls["calculator"])
        await client.connect(strict=True)
        try:
            tool = next(t for t in client.get_tools() if t.name == "mcp:calculator:calculate")
            result = await tool.execute(expression="6 * 7")
            assert "42" in result.content
        finally:
            await client.disconnect()
    finally:
        await sup.aclose()


async def test_dead_server_is_respawned_on_next_resolution() -> None:
    sup = BuiltinMCPSupervisor(("time",))
    try:
        url1 = await sup.ensure("time")
        # Kill the underlying process out from under the supervisor.
        state = sup._states["time"]
        state.proc.kill()  # type: ignore[union-attr]
        state.proc.wait(timeout=5)  # type: ignore[union-attr]
        assert sup.running_server_count == 0
        # Next resolution re-spawns (a fresh port).
        url2 = await sup.ensure("time")
        assert url2 is not None
        assert sup.running_server_count == 1
        assert url2 != url1  # fresh port on re-spawn
    finally:
        await sup.aclose()


async def test_spawned_child_cannot_see_a_parent_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    # R9-258 end to end: read the real child's environment from the OS, not the dict
    # the supervisor built, and prove a sentinel secret in the api env is not there.
    psutil = pytest.importorskip("psutil")
    sentinels = {
        "DATABASE_URL": "postgres://su:r9-258-sentinel@db/persona",
        "STRIPE_SECRET_KEY": "sk_live_r9-258-sentinel",
        "OPENAI_API_KEY": "sk-r9-258-sentinel",
    }
    for name, value in sentinels.items():
        monkeypatch.setenv(name, value)
    sup = BuiltinMCPSupervisor(("calculator",))
    try:
        assert await sup.ensure("calculator") is not None  # the child still boots
        proc = sup._states["calculator"].proc
        assert proc is not None
        child_env = psutil.Process(proc.pid).environ()
        upper = {name.upper() for name in child_env}
        assert not upper & set(sentinels)
        assert not any("r9-258-sentinel" in value for value in child_env.values())
        assert "PATH" in upper
    finally:
        await sup.aclose()
