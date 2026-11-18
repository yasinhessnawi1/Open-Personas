"""Built-in MCP servers (Spec 27, D-27-1 / D-27-2).

Persona authors a small set of MCP servers as thin FastMCP apps served over
Streamable HTTP and bound to loopback only. Each server module exposes a
``build(host, port) -> FastMCP`` factory; this package assembles them into
:data:`SERVER_BUILDERS`, the registry the ``python -m persona.tools.mcp.builtin``
entrypoint (:mod:`persona.tools.mcp.builtin.__main__`) and the API launcher
dispatch against by name.

The launch set (catalog ``kind="builtin"``): ``time``, ``calculator``,
``filesystem`` (default-enabled) + ``weather`` (opt-in). External servers
(fetch / github) ship NO code and are absent here — they are configured via
``PERSONA_MCP_SERVERS`` (D-27-11).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.child_env import build_child_env
from persona.tools.mcp.builtin._harness import (
    DEFAULT_BIND_HOST,
    STREAMABLE_HTTP_TRANSPORT,
    run_streamable_http,
    serve,
)
from persona.tools.mcp.builtin.calculator_server import CHILD_ENV as _CALCULATOR_ENV
from persona.tools.mcp.builtin.calculator_server import build as _build_calculator
from persona.tools.mcp.builtin.filesystem_server import CHILD_ENV as _FILESYSTEM_ENV
from persona.tools.mcp.builtin.filesystem_server import build as _build_filesystem
from persona.tools.mcp.builtin.time_server import CHILD_ENV as _TIME_ENV
from persona.tools.mcp.builtin.time_server import build as _build_time
from persona.tools.mcp.builtin.weather_server import CHILD_ENV as _WEATHER_ENV
from persona.tools.mcp.builtin.weather_server import build as _build_weather

if TYPE_CHECKING:
    from collections.abc import Mapping

    from persona.tools.mcp.builtin._harness import ServerBuilder

__all__ = [
    "DEFAULT_BIND_HOST",
    "SERVER_BUILDERS",
    "SERVER_CHILD_ENV",
    "STREAMABLE_HTTP_TRANSPORT",
    "child_env",
    "run_streamable_http",
    "serve",
]

#: Registry of built-in server name → ``(host, port) -> FastMCP`` builder.
#: Keys MUST match the catalog's authored (``kind="builtin"``) names.
SERVER_BUILDERS: dict[str, ServerBuilder] = {
    "time": _build_time,
    "calculator": _build_calculator,
    "filesystem": _build_filesystem,
    "weather": _build_weather,
}

#: Registry of built-in server name -> the env names that server needs beyond the
#: shared base (R9-258). Keys MUST match :data:`SERVER_BUILDERS`; each set is the
#: ``CHILD_ENV`` its server module declares.
SERVER_CHILD_ENV: dict[str, frozenset[str]] = {
    "time": _TIME_ENV,
    "calculator": _CALCULATOR_ENV,
    "filesystem": _FILESYSTEM_ENV,
    "weather": _WEATHER_ENV,
}


def child_env(
    name: str, parent: Mapping[str, str], overrides: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The env to start built-in server ``name`` with: allow-listed names only (R9-258).

    The shared base plus the server's own ``CHILD_ENV``, taken from ``parent`` (the
    api env), plus the launcher's per-spawn ``overrides``. Everything else, every
    DSN, key and token the api holds, stays in the parent. An unknown name gets the
    base alone (it cannot be served anyway).
    """
    return build_child_env(SERVER_CHILD_ENV.get(name, frozenset()), parent, overrides)
