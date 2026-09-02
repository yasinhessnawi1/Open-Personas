"""Entrypoint for launching a built-in MCP server as a subprocess (Spec 27 T5).

The API launcher (:mod:`persona_api.mcp.builtin_launcher`) spawns built-in
servers via ``python -m persona.tools.mcp.builtin <name> --host H --port P``.
Keeping the launch surface a module entrypoint (not a console script) means the
launcher uses the very interpreter the API runs under — the subprocess inherits
the parent's user/uid (D-27-12: in production the API runs as the non-root
persona user, so its children do too).
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from persona.tools.mcp.builtin._harness import DEFAULT_BIND_HOST, serve

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> None:
    """Parse argv and run the named built-in server over Streamable HTTP."""
    parser = argparse.ArgumentParser(
        prog="python -m persona.tools.mcp.builtin",
        description="Run a Persona built-in MCP server over Streamable HTTP (loopback only).",
    )
    parser.add_argument("name", help="Built-in server name (e.g. time, calculator, filesystem).")
    parser.add_argument(
        "--host",
        default=DEFAULT_BIND_HOST,
        help=f"Bind address (default {DEFAULT_BIND_HOST}; loopback only, D-27-12).",
    )
    parser.add_argument("--port", type=int, required=True, help="TCP port to bind.")
    args = parser.parse_args(argv)
    serve(args.name, host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in integration tests
    main()
