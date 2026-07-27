"""Tests for the MCPClient lifecycle wrapper (T11)."""

# ruff: noqa: ANN401, ARG001, ARG002, ERA001, SLF001
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona.errors import MCPServerUnavailableError
from persona.tools.audit import MemoryToolAuditLogger
from persona.tools.mcp.client import MCPClient, load_mcp_clients
from persona.tools.protocol import AsyncTool

# Section: SDK shape mocks


def _fake_tools_result(*tool_names: str) -> SimpleNamespace:
    return SimpleNamespace(
        tools=[
            SimpleNamespace(
                name=n,
                description=f"Tool {n}",
                inputSchema={"type": "object", "properties": {"q": {"type": "string"}}},
            )
            for n in tool_names
        ]
    )


def _patch_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tools: list[str] | None = None,
    transport_raises: Exception | None = None,
    initialize_raises: Exception | None = None,
    list_tools_raises: Exception | None = None,
) -> MagicMock:
    """Patch the streamablehttp_client + ClientSession contexts the MCP client uses.

    Returns a mock object that captures the ClientSession instance for
    assertion (e.g., to verify list_tools was awaited).
    """
    tool_names = tools or ["search"]

    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_transport(_url: str) -> Any:  # noqa: ANN401
        if transport_raises is not None:
            raise transport_raises
        # Return (read, write, get_session_id) tuple.
        yield (MagicMock(name="read"), MagicMock(name="write"), MagicMock(name="get_sid"))

    @asynccontextmanager
    async def fake_session_ctx(_read: Any, _write: Any) -> Any:  # noqa: ANN401
        session = SimpleNamespace(
            initialize=AsyncMock(side_effect=initialize_raises),
            list_tools=AsyncMock(
                side_effect=list_tools_raises,
                return_value=_fake_tools_result(*tool_names),
            ),
            call_tool=AsyncMock(),
        )
        captured["session"] = session
        yield session

    # Patch the import targets inside MCPClient.connect — it does a local
    # import of `from mcp.client.streamable_http import streamablehttp_client`
    # and `from mcp import ClientSession`, so we patch the names that the
    # SDK exposes at those module paths.
    import mcp
    import mcp.client.streamable_http as shttp

    monkeypatch.setattr(shttp, "streamablehttp_client", fake_transport)
    # ClientSession is a class — substitute with a callable that returns the
    # async-context-manager.
    monkeypatch.setattr(mcp, "ClientSession", fake_session_ctx)

    return MagicMock(captured=captured)


# Section: connect happy path


class TestConnectHappy:
    @pytest.mark.asyncio
    async def test_connect_discovers_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_sdk(monkeypatch, tools=["search", "fetch"])
        client = MCPClient(server_name="legal", server_url="https://x/mcp")
        await client.connect()
        assert client.is_connected
        tools = client.get_tools()
        assert len(tools) == 2
        for tool in tools:
            assert isinstance(tool, AsyncTool)
        names = {t.name for t in tools}
        assert names == {"mcp:legal:search", "mcp:legal:fetch"}
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_connect_emits_audit_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_sdk(monkeypatch)
        audit = MemoryToolAuditLogger()
        client = MCPClient(
            server_name="legal",
            server_url="https://x/mcp",
            audit_logger=audit,
            persona_id="bot",
        )
        await client.connect()
        # One audit event: action="connect".
        assert len(audit.events) == 1
        ev = audit.events[0]
        assert ev.action == "connect"
        assert ev.resource == "legal"
        assert ev.tool_name == "mcp:legal"
        assert ev.metadata["transport"] == "streamable_http"
        assert ev.persona_id == "bot"
        await client.disconnect()


# Section: disconnect


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_emits_audit_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_sdk(monkeypatch)
        audit = MemoryToolAuditLogger()
        client = MCPClient(server_name="legal", server_url="https://x/mcp", audit_logger=audit)
        await client.connect()
        await client.disconnect(reason="user_close")

        assert not client.is_connected
        assert client.get_tools() == []
        actions = [e.action for e in audit.events]
        assert actions == ["connect", "disconnect"]
        assert audit.events[1].metadata["reason"] == "user_close"

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_sdk(monkeypatch)
        audit = MemoryToolAuditLogger()
        client = MCPClient(server_name="x", server_url="https://x/mcp", audit_logger=audit)
        await client.connect()
        await client.disconnect()
        await client.disconnect()  # second call is a no-op
        actions = [e.action for e in audit.events]
        assert actions == ["connect", "disconnect"]  # NOT duplicated


# Section: strict vs graceful


class TestStrictModeFailure:
    @pytest.mark.asyncio
    async def test_strict_raises_on_transport_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_sdk(monkeypatch, transport_raises=ConnectionError("dns failed"))
        audit = MemoryToolAuditLogger()
        client = MCPClient(server_name="dead", server_url="https://nowhere/mcp", audit_logger=audit)
        with pytest.raises(MCPServerUnavailableError) as exc_info:
            await client.connect(strict=True)
        assert "dead" in str(exc_info.value)
        # Audit emitted server_unavailable.
        assert audit.events[0].action == "server_unavailable"
        assert audit.events[0].is_error is True

    @pytest.mark.asyncio
    async def test_strict_raises_on_initialize_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_sdk(monkeypatch, initialize_raises=RuntimeError("init bad"))
        client = MCPClient(server_name="srv", server_url="https://x/mcp")
        with pytest.raises(MCPServerUnavailableError):
            await client.connect(strict=True)


class TestGracefulModeFailure:
    @pytest.mark.asyncio
    async def test_nonstrict_omits_tools_on_transport_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_sdk(monkeypatch, transport_raises=ConnectionError("dns failed"))
        audit = MemoryToolAuditLogger()
        client = MCPClient(
            server_name="dead",
            server_url="https://nowhere/mcp",
            audit_logger=audit,
        )
        # No exception.
        await client.connect(strict=False)
        # Not connected; no tools.
        assert not client.is_connected
        assert client.get_tools() == []
        # server_unavailable audited.
        assert audit.events[0].action == "server_unavailable"
        assert audit.events[0].is_error is True

    @pytest.mark.asyncio
    async def test_nonstrict_recovers_after_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A second connect attempt after a graceful failure can succeed.
        _patch_sdk(monkeypatch, transport_raises=ConnectionError("dns"))
        client = MCPClient(server_name="x", server_url="https://x/mcp")
        await client.connect(strict=False)
        assert not client.is_connected

        # Replace the transport mock so the next connect succeeds.
        _patch_sdk(monkeypatch, tools=["search"])
        await client.connect(strict=False)
        assert client.is_connected
        assert len(client.get_tools()) == 1
        await client.disconnect()


# Section: R9-042 — a refused connection surfaces as CancelledError, not Exception
#
# These deliberately do NOT use `_patch_sdk` (which only ever raises a plain
# `Exception` from the transport). The real bug is that `mcp.client.streamable_http`
# opens its transport inside an `anyio.create_task_group()`: when the real TCP
# connect is refused, anyio cancels the task group's host task too, so the
# exception observed by `MCPClient._open_session` is a BARE
# `asyncio.CancelledError` — invisible to `except Exception`. Reproducing that
# shape needs the REAL SDK transport against a REAL closed/hanging socket.


def _unused_tcp_port() -> int:
    """A port nothing is listening on: bind ephemeral, read it back, close it.

    Guarantees an immediate ECONNREFUSED on connect (no hang, no privileges
    needed, portable) — unlike a fixed low port, which may be firewalled
    (hangs) rather than refused in some sandboxes/CI.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestCancelledErrorDegrade:
    """R9-042: a refused connection must degrade (strict=False) / fail cleanly
    (strict=True) — never crash uncaught as a bare CancelledError."""

    @pytest.mark.asyncio
    async def test_nonstrict_degrades_on_refused_connection(self) -> None:
        port = _unused_tcp_port()
        audit = MemoryToolAuditLogger()
        client = MCPClient(
            server_name="down",
            server_url=f"http://127.0.0.1:{port}/mcp",
            audit_logger=audit,
        )
        # No exception — this is exactly what crashed before the fix (a bare
        # CancelledError propagated straight out of connect(strict=False)).
        await client.connect(strict=False)
        assert not client.is_connected
        assert client.get_tools() == []
        assert audit.events[0].action == "server_unavailable"
        assert audit.events[0].is_error is True

    @pytest.mark.asyncio
    async def test_strict_raises_mcpserverunavailable_on_refused_connection(self) -> None:
        port = _unused_tcp_port()
        client = MCPClient(server_name="down", server_url=f"http://127.0.0.1:{port}/mcp")
        # Previously this leaked a bare CancelledError instead of the documented
        # MCPServerUnavailableError contract.
        with pytest.raises(MCPServerUnavailableError):
            await client.connect(strict=True)

    @pytest.mark.asyncio
    async def test_recovers_after_refused_connection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A graceful degrade must leave the client reusable, same as the
        # existing mocked-Exception recovery case.
        port = _unused_tcp_port()
        client = MCPClient(server_name="x", server_url=f"http://127.0.0.1:{port}/mcp")
        await client.connect(strict=False)
        assert not client.is_connected

        _patch_sdk(monkeypatch, tools=["search"])
        await client.connect(strict=False)
        assert client.is_connected
        assert len(client.get_tools()) == 1
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_genuine_task_cancellation_still_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The invariant: a genuinely-cancelled OUTER task still raises
        CancelledError — R9-042's fix must never swallow a real cancel.

        Drives the REAL `connect()` code path (the `except asyncio.CancelledError`
        branch + `_drain_after_cancelled_connect`) without a real socket: the
        transport's `__aenter__` hangs on an ``asyncio.Event`` that's only ever
        cleared by an external `task.cancel()` — mirroring a slow-to-connect
        transport (TLS handshake still in flight) that the CALLER gives up on
        (shutdown, or a caller's `wait_for` timeout on `connect()` itself).
        Since the exit stack never entered anything, draining it on the caught
        CancelledError comes back clean -> no real error underneath -> the fix
        must re-raise, not degrade.

        (Deliberately mocked rather than a real hung TCP listener: two
        back-to-back tests each spinning up a real anyio task group against a
        live socket reproducibly deadlocked this repo's pytest-asyncio
        event-loop teardown — an environment/SDK cleanup interaction unrelated
        to the fix. This mock exercises the identical `connect()` branch.)
        """
        import mcp
        import mcp.client.streamable_http as shttp

        never_connects = asyncio.Event()

        @asynccontextmanager
        async def fake_transport_hangs(_url: str, **_kwargs: Any) -> Any:
            await never_connects.wait()
            yield (MagicMock(), MagicMock(), MagicMock())  # pragma: no cover — unreachable

        monkeypatch.setattr(shttp, "streamablehttp_client", fake_transport_hangs)
        monkeypatch.setattr(mcp, "ClientSession", MagicMock())

        client = MCPClient(server_name="hang", server_url="https://slow/mcp")
        task = asyncio.create_task(client.connect(strict=False))
        await asyncio.sleep(0.05)  # let it get stuck inside transport __aenter__
        assert not task.done(), "expected the connect() task to still be hung"
        task.cancel()
        # NOTE: deliberately try/except, not `pytest.raises` — wrapping this
        # particular `await task` in `pytest.raises(asyncio.CancelledError)`
        # deadlocked under this repo's pytest-asyncio setup (reproduced
        # standalone too; unrelated to the R9-042 fix under test).
        raised = False
        try:
            await task
        except asyncio.CancelledError:
            raised = True
        assert raised, "genuine external cancellation must still raise CancelledError"


# Section: R9-049 — _reopen() shares R9-042's unguarded connect shape
#
# The R8 reconnect-on-401 path (_reopen, called from _reauth_and_reconnect) opens
# a fresh transport the exact same way connect() does — a refused/dropped
# reconnect target surfaces as a bare CancelledError too. Mirrors
# TestCancelledErrorDegrade above, but drives _reopen() directly (the state a
# post-401 reconnect attempt is in: previously connected, headers refreshed).


class TestReopenCancelledErrorDegrade:
    """R9-049: a refused reconnect target must degrade (return None,
    fail-closed) — never crash uncaught as a bare CancelledError."""

    @pytest.mark.asyncio
    async def test_reopen_degrades_on_refused_connection(self) -> None:
        port = _unused_tcp_port()
        client = MCPClient(server_name="gh", server_url=f"http://127.0.0.1:{port}/mcp")
        # The post-401 state _reauth_and_reconnect leaves us in before calling
        # _reopen(): was connected, headers just refreshed by the reauth callback.
        client._connected = True
        client._headers = {"Authorization": "Bearer NEW"}
        # No exception — this is exactly what crashed before the fix (a bare
        # CancelledError propagating straight out of _reopen()).
        result = await client._reopen()
        assert result is None
        assert not client.is_connected

    @pytest.mark.asyncio
    async def test_reopen_recovers_after_refused_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A graceful degrade must leave the client reusable for a later reconnect.
        port = _unused_tcp_port()
        client = MCPClient(server_name="gh", server_url=f"http://127.0.0.1:{port}/mcp")
        client._connected = True
        assert await client._reopen() is None
        assert not client.is_connected

        _patch_sdk(monkeypatch, tools=["search"])
        session = await client._reopen()
        assert session is not None
        assert client.is_connected
        await client.disconnect()

    @pytest.mark.asyncio
    async def test_reopen_genuine_task_cancellation_still_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The invariant: a genuinely-cancelled OUTER task calling _reopen()
        still raises CancelledError — the fix must never swallow a real cancel."""
        import mcp
        import mcp.client.streamable_http as shttp

        never_connects = asyncio.Event()

        @asynccontextmanager
        async def fake_transport_hangs(_url: str, **_kwargs: Any) -> Any:
            await never_connects.wait()
            yield (MagicMock(), MagicMock(), MagicMock())  # pragma: no cover — unreachable

        monkeypatch.setattr(shttp, "streamablehttp_client", fake_transport_hangs)
        monkeypatch.setattr(mcp, "ClientSession", MagicMock())

        client = MCPClient(server_name="hang", server_url="https://slow/mcp")
        client._connected = True
        task = asyncio.create_task(client._reopen())
        await asyncio.sleep(0.05)  # let it get stuck inside transport __aenter__
        assert not task.done(), "expected the _reopen() task to still be hung"
        task.cancel()
        raised = False
        try:
            await task
        except asyncio.CancelledError:
            raised = True
        assert raised, "genuine external cancellation must still raise CancelledError"


# Section: load_mcp_clients helper


class TestLoadMCPClients:
    @pytest.mark.asyncio
    async def test_loads_multiple_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_sdk(monkeypatch, tools=["search"])
        clients = await load_mcp_clients(
            {"a": "https://a/mcp", "b": "https://b/mcp"},
            strict=False,
        )
        assert len(clients) == 2
        for c in clients:
            await c.disconnect()

    @pytest.mark.asyncio
    async def test_graceful_skips_unreachable_in_nonstrict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_sdk(monkeypatch, transport_raises=ConnectionError("dns"))
        audit = MemoryToolAuditLogger()
        clients = await load_mcp_clients(
            {"a": "https://nowhere/mcp"},
            audit_logger=audit,
            strict=False,
        )
        assert len(clients) == 1
        assert not clients[0].is_connected
        assert clients[0].get_tools() == []
        assert audit.events[0].action == "server_unavailable"


# Section: AsyncTool integration via Toolbox


class TestMCPToolsIntegrateWithToolbox:
    @pytest.mark.asyncio
    async def test_mcp_tools_dispatchable_via_toolbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from persona.schema.tools import ToolCall
        from persona.tools.toolbox import Toolbox

        sdk = _patch_sdk(monkeypatch, tools=["search"])
        client = MCPClient(server_name="legal", server_url="https://x/mcp")
        await client.connect()

        # Fake the call_tool result so dispatch returns something useful.
        session = sdk.captured["session"]
        session.call_tool.return_value = SimpleNamespace(
            content=[SimpleNamespace(text="42 results", type="text")],
            isError=False,
            structuredContent=None,
        )

        toolbox = Toolbox(client.get_tools(), allow_list=["mcp:legal:search"])
        result = await toolbox.dispatch(
            ToolCall(name="mcp:legal:search", args={"q": "rent"}, call_id="c1")
        )
        assert result.is_error is False
        assert result.content == "42 results"
        session.call_tool.assert_awaited_once_with("search", arguments={"q": "rent"})

        await client.disconnect()
