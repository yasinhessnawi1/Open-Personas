"""R8 T8 — reconnect-on-401 in the MCP connection loop (R8-D-5).

A mid-session 401 means the static OAuth bearer header outlived the access token. The
adapter asks the client to refresh+rotate + rebuild the transport ONCE, then retries on
the fresh session. Fail-closed backstop: a declined reauth, or a retry that still 401s,
returns a graceful error ToolResult — never a retry loop.
"""

# ruff: noqa: ANN401, ARG001, SLF001
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from persona.tools.mcp.adapter import MCPToolAdapter, _is_auth_error
from persona.tools.mcp.client import MCPClient


def _tool_def(name: str = "do_thing") -> SimpleNamespace:
    return SimpleNamespace(name=name, description="d", inputSchema={"type": "object"})


def _result(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=text, type="text")], isError=False, structuredContent=None
    )


class _Http401Error(Exception):
    """An SDK-ish error carrying a 401 response, like a wrapped httpx.HTTPStatusError."""

    def __init__(self) -> None:
        super().__init__("Client error '401 Unauthorized'")
        self.response = SimpleNamespace(status_code=401)


class TestIsAuthError:
    def test_detects_response_status_401(self) -> None:
        assert _is_auth_error(_Http401Error()) is True

    def test_detects_status_code_attr(self) -> None:
        exc = Exception("boom")
        exc.status_code = 401  # type: ignore[attr-defined]
        assert _is_auth_error(exc) is True

    def test_detects_via_message(self) -> None:
        assert _is_auth_error(Exception("got 401 back")) is True
        assert _is_auth_error(Exception("Unauthorized")) is True

    def test_detects_in_cause_chain(self) -> None:
        chained: Exception
        try:
            raise RuntimeError("tool dispatch failed") from _Http401Error()
        except RuntimeError as outer:
            chained = outer
        assert _is_auth_error(chained) is True

    def test_non_auth_error_is_false(self) -> None:
        assert _is_auth_error(Exception("connection reset")) is False


class TestAdapterReconnect:
    @pytest.mark.asyncio
    async def test_401_triggers_reauth_and_retries_on_new_session(self) -> None:
        # First session raises 401; the reauth returns a fresh session that succeeds.
        stale = SimpleNamespace(call_tool=AsyncMock(side_effect=_Http401Error()))
        fresh = SimpleNamespace(call_tool=AsyncMock(return_value=_result("recovered")))
        reauth_calls = {"n": 0}

        async def on_auth_error() -> Any:
            reauth_calls["n"] += 1
            return fresh

        adapter = MCPToolAdapter(
            server_name="gh", session=stale, tool_def=_tool_def(), on_auth_error=on_auth_error
        )
        out = await adapter.execute(q="x")
        assert out.is_error is False
        assert out.content == "recovered"
        assert reauth_calls["n"] == 1
        fresh.call_tool.assert_awaited_once()  # retried on the fresh session

    @pytest.mark.asyncio
    async def test_declined_reauth_fails_closed(self) -> None:
        stale = SimpleNamespace(call_tool=AsyncMock(side_effect=_Http401Error()))

        async def on_auth_error() -> Any:
            return None  # reauth declined → not-connected

        adapter = MCPToolAdapter(
            server_name="gh", session=stale, tool_def=_tool_def(), on_auth_error=on_auth_error
        )
        out = await adapter.execute(q="x")
        assert out.is_error is True

    @pytest.mark.asyncio
    async def test_retry_still_401_fails_closed_no_loop(self) -> None:
        stale = SimpleNamespace(call_tool=AsyncMock(side_effect=_Http401Error()))
        still_bad = SimpleNamespace(call_tool=AsyncMock(side_effect=_Http401Error()))
        calls = {"n": 0}

        async def on_auth_error() -> Any:
            calls["n"] += 1
            return still_bad

        adapter = MCPToolAdapter(
            server_name="gh", session=stale, tool_def=_tool_def(), on_auth_error=on_auth_error
        )
        out = await adapter.execute(q="x")
        assert out.is_error is True
        assert calls["n"] == 1  # exactly one reauth attempt — no loop

    @pytest.mark.asyncio
    async def test_no_reauth_hook_preserves_graceful_error(self) -> None:
        stale = SimpleNamespace(call_tool=AsyncMock(side_effect=_Http401Error()))
        adapter = MCPToolAdapter(server_name="gh", session=stale, tool_def=_tool_def())
        out = await adapter.execute(q="x")
        assert out.is_error is True


class TestClientReauthOnce:
    @pytest.mark.asyncio
    async def test_reauth_is_once_per_lifetime(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reauth = AsyncMock(return_value={"Authorization": "Bearer NEW"})
        client = MCPClient(server_name="gh", server_url="https://x/mcp", reauth=reauth)
        client._connected = True
        fresh_session = SimpleNamespace(name="fresh")

        async def fake_reopen() -> Any:
            client._session = fresh_session
            return fresh_session

        monkeypatch.setattr(client, "_reopen", fake_reopen)

        first = await client._reauth_and_reconnect()
        assert first is fresh_session
        # A second concurrentish call reuses the result — reauth is NOT invoked again.
        second = await client._reauth_and_reconnect()
        assert second is fresh_session
        assert reauth.await_count == 1

    @pytest.mark.asyncio
    async def test_declined_reauth_returns_none(self) -> None:
        reauth = AsyncMock(return_value=None)  # fail-closed
        client = MCPClient(server_name="gh", server_url="https://x/mcp", reauth=reauth)
        client._connected = True
        assert await client._reauth_and_reconnect() is None

    @pytest.mark.asyncio
    async def test_no_reauth_callback_returns_none(self) -> None:
        client = MCPClient(server_name="gh", server_url="https://x/mcp")
        assert await client._reauth_and_reconnect() is None
