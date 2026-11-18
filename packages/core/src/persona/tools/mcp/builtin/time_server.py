"""Built-in ``time`` MCP server (Spec 27 T6, D-27-2).

A thin FastMCP Streamable-HTTP server that exposes the in-tree ``datetime`` tool
over MCP. It does NOT re-implement date logic: it delegates to
:func:`persona.tools.builtin.datetime.make_datetime_tool` (the Spec-26
stdlib + ``tzdata`` implementation) and maps its :class:`ToolResult` to the MCP
return — so the zone resolution, ISO-8601 parsing, and the four operations stay
single-sourced (DRY). Read-only, no network, no credentials.

Exposed as ``mcp:time:datetime`` to personas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from persona.tools.builtin.datetime import make_datetime_tool

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["CHILD_ENV", "build"]

#: Env names this server needs beyond the shared base (R9-258). None: it reads the
#: clock and the bundled tzdata; TZ and PYTHONTZPATH are already in the base.
CHILD_ENV: frozenset[str] = frozenset()

# Build the underlying AsyncTool once per process — pure + stateless, so a
# module-level instance is safe and avoids rebuilding it on every call.
_DATETIME_TOOL = make_datetime_tool()


def build(host: str, port: int) -> FastMCP:
    """Build (do not run) the ``time`` FastMCP server bound to ``host:port``."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("time", host=host, port=port)

    @server.tool()
    async def datetime(
        operation: Literal["now", "convert", "add", "diff"] = "now",
        timezone: str = "UTC",
        datetime: str | None = None,  # noqa: A002 — model-facing arg mirrors the in-tree tool
        to_timezone: str | None = None,
        amount: int | None = None,
        unit: Literal["weeks", "days", "hours", "minutes", "seconds"] | None = None,
        end: str | None = None,
    ) -> str:
        """Work with dates and times precisely (current time, timezone conversion,
        date arithmetic). operation='now' gives the current time in a timezone;
        'convert' converts a datetime between timezones; 'add' adds/subtracts
        (amount + unit); 'diff' gives the difference between two datetimes.
        Timezones are IANA names ('UTC', 'Europe/Oslo'); datetimes are ISO 8601.
        """
        result = await _DATETIME_TOOL.execute(
            operation=operation,
            timezone=timezone,
            datetime=datetime,
            to_timezone=to_timezone,
            amount=amount,
            unit=unit,
            end=end,
        )
        if result.is_error:
            # Surface as an MCP tool error (FastMCP maps a raised exception to an
            # isError result; the adapter turns that back into ToolResult.is_error).
            raise ValueError(result.content)
        return result.content

    return server
