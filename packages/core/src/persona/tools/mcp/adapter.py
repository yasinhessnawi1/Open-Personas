"""MCP tool adapter — wraps an MCP server tool as an :class:`AsyncTool`.

Per spec §7.2 and D-03-19, each MCP-discovered tool becomes an
:class:`AsyncTool` named ``mcp:{server_name}:{tool_name}`` so the
Toolbox's literal allow-list (Phase 1 refinement #4) handles it
unambiguously.

The adapter calls ``ClientSession.call_tool(name, arguments=kwargs)``
on the underlying MCP session and maps the result to
:class:`ToolResult`. If the connection dies mid-call, the adapter
returns ``ToolResult(is_error=True, content="MCP server disconnected")``
per spec §7.3 — no exception escapes.

Per-call dispatch audits are skipped per D-03-21. The lifecycle
events (connect / disconnect / server_unavailable) are emitted by
:class:`persona.tools.mcp.client.MCPClient`, not the adapter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona.logging import get_logger
from persona.schema.tools import ToolResult

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mcp import ClientSession
    from mcp.types import Tool as MCPToolDef

__all__ = ["MCPToolAdapter"]

_logger = get_logger("tools.mcp.adapter")


def _is_auth_error(exc: BaseException) -> bool:
    """True iff ``exc`` (or its cause/context chain) is an HTTP 401 (Spec R8, R8-D-5).

    The MCP SDK surfaces a transport 401 wrapped in its own error types, so walk the
    ``__cause__``/``__context__`` chain and check for a ``response.status_code == 401``,
    a ``status_code == 401`` attribute, or a ``401``/``Unauthorized`` marker in the text.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        response = getattr(cur, "response", None)
        if response is not None and getattr(response, "status_code", None) == 401:
            return True
        if getattr(cur, "status_code", None) == 401:
            return True
        text = str(cur)
        if "401" in text or "Unauthorized" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


class MCPToolAdapter:
    """Wraps a single MCP server tool as an :class:`AsyncTool`.

    Attributes are class-stamped (not properties) so the structural
    Protocol check (``isinstance(obj, AsyncTool)``) sees them.

    Args:
        server_name: The MCP server identifier (config key); the
            ``mcp:<server>:`` prefix in the resulting tool name.
        session: The active :class:`mcp.ClientSession`. Lifecycle is
            managed by :class:`MCPClient`; the adapter only holds a
            reference and never opens/closes the session.
        tool_def: The :class:`mcp.types.Tool` returned by ``list_tools``.
    """

    def __init__(
        self,
        *,
        server_name: str,
        session: ClientSession,
        tool_def: MCPToolDef,
        on_auth_error: Callable[[], Awaitable[ClientSession | None]] | None = None,
    ) -> None:
        self._server_name = server_name
        self._session = session
        self._mcp_tool_name = tool_def.name
        # Spec R8 (R8-D-5): reconnect-on-401 hook. On a mid-session 401 the adapter asks
        # the owning client to refresh+rotate the token and rebuild the transport; the
        # callback returns the fresh live session to retry on (or None → fail closed).
        # Default None keeps the pre-R8 adapter behaviour byte-identical.
        self._on_auth_error = on_auth_error

        # Stamp the AsyncTool surface as instance attributes — Protocols
        # accept either properties or plain attributes.
        self.name = f"mcp:{server_name}:{tool_def.name}"
        self.description = tool_def.description or ""
        # MCP gives us a JSON-Schema dict (`inputSchema`). Anthropic + OpenAI
        # accept this dialect directly (research §3.5 / §4.2).
        self.parameters_schema: dict[str, Any] = dict(tool_def.inputSchema or {})

    async def execute(self, **kwargs: Any) -> ToolResult:  # noqa: ANN401
        try:
            result = await self._session.call_tool(self._mcp_tool_name, arguments=kwargs)
        except Exception as e:  # noqa: BLE001 — broad envelope; tool never raises
            # Spec R8 (R8-D-5): a mid-session 401 means the static OAuth header outlived
            # the access token. Ask the client to refresh+rotate + rebuild the transport
            # ONCE, then retry on the fresh session. A retry that still fails (or a
            # declined reauth) falls through to the graceful error path — no loop.
            if self._on_auth_error is not None and _is_auth_error(e):
                new_session = await self._on_auth_error()
                if new_session is not None:
                    self._session = new_session
                    try:
                        result = await new_session.call_tool(self._mcp_tool_name, arguments=kwargs)
                    except Exception as retry_exc:  # noqa: BLE001 — one retry, then fail closed
                        return self._error_result(retry_exc)
                    return self._to_result(result)
            # Includes connection-died errors from the SDK (anyio.EndOfStream,
            # ClosedResourceError, httpx.HTTPError) — all become a graceful
            # ToolResult per spec §7.3.
            return self._error_result(e)
        return self._to_result(result)

    def _error_result(self, e: BaseException) -> ToolResult:
        """Map a call exception to a graceful error :class:`ToolResult` (spec §7.3)."""
        _logger.warning(
            "mcp tool call failed",
            server=self._server_name,
            tool=self._mcp_tool_name,
            error=type(e).__name__,
        )
        err_type = type(e).__name__
        msg = str(e) or ""
        # Disconnection-like errors get the canonical message.
        disconnect_markers = ("ClosedResource", "EndOfStream", "Disconnect")
        if any(marker in err_type for marker in disconnect_markers):
            return ToolResult(
                tool_name=self.name,
                content="MCP server disconnected",
                is_error=True,
            )
        return ToolResult(
            tool_name=self.name,
            content=f"{err_type}: {msg}",
            is_error=True,
        )

    def _to_result(self, result: Any) -> ToolResult:  # noqa: ANN401
        """Aggregate an MCP ``CallToolResult`` into a :class:`ToolResult`.

        MCP returns a list of content blocks (TextContent, ImageContent, etc.); we
        concatenate the text content for ``content`` and surface structured content via
        ``data`` when present.
        """
        text_parts: list[str] = []
        for block in result.content or []:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                text_parts.append(text)

        content = "\n".join(text_parts) if text_parts else ""
        data: dict[str, Any] | None = None
        structured = getattr(result, "structuredContent", None)
        if structured:
            data = dict(structured) if isinstance(structured, dict) else {"value": structured}

        return ToolResult(
            tool_name=self.name,
            content=content,
            data=data,
            is_error=bool(getattr(result, "isError", False)),
        )
