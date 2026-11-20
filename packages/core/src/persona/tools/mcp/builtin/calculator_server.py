"""Built-in ``calculator`` MCP server (Spec 27 T7, D-27-2).

A thin FastMCP Streamable-HTTP server that exposes the Spec-26 hardened
calculator over MCP. It does NOT re-implement evaluation: it delegates to
:func:`persona.tools.builtin.calculator.make_calculator_tool`, so the AST node
whitelist (no ``eval``, no attribute access → no RCE) and the DoS guards
(expression-length / node-count / depth / exponent / factorial caps) are
single-sourced. No code sandbox, no network, no credentials.

Exposed as ``mcp:calculator:calculate`` to personas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.tools.builtin.calculator import make_calculator_tool

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["CHILD_ENV", "build"]

#: Env names this server needs beyond the shared base (R9-258). None: pure arithmetic.
CHILD_ENV: frozenset[str] = frozenset()

# Build the underlying AsyncTool once per process — pure + stateless.
_CALCULATOR_TOOL = make_calculator_tool()


def build(host: str, port: int) -> FastMCP:
    """Build (do not run) the ``calculator`` FastMCP server bound to ``host:port``."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("calculator", host=host, port=port)

    @server.tool()
    async def calculate(expression: str) -> str:
        """Evaluate an exact arithmetic / math expression. Supports + - * / // % **,
        parentheses, the constants pi/e/tau, and math functions (sqrt, sin, log,
        factorial, ...). No variables, no code execution. Example: 'sqrt(2) * 3 + 1'.
        """
        result = await _CALCULATOR_TOOL.execute(expression=expression)
        if result.is_error:
            raise ValueError(result.content)
        return result.content

    return server
