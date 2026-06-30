"""Built-in ``filesystem`` MCP server (Spec 27 T8, D-27-2 / D-27-12).

A thin FastMCP Streamable-HTTP server that exposes sandboxed file read/write over
MCP. It is the ONE built-in with write capability, so it is contained in depth:

1. **Loopback bind** — the launcher binds ``127.0.0.1`` only (D-27-12), so the
   server is never reachable off-box.
2. **Path-traversal guard** — read/write delegate to the in-tree
   ``file_read`` / ``file_write`` tools, whose
   :func:`persona.tools._sandbox.resolve_sandbox_path` rejects ``..``, absolute
   paths, NULL bytes, and symlinks escaping the sandbox, and which open with
   ``O_NOFOLLOW``. The guard is single-sourced — this server adds no new file I/O.
3. **Non-root user** — the launcher runs the subprocess as the non-root persona
   user (D-27-12), so even a guard bypass cannot act as the API user.

The sandbox root is the **per-(owner, persona) scoped root** threaded in at spawn
by the supervisor over ``PERSONA_FILESYSTEM_SCOPE_ROOT`` (Spec P4) — a subprocess
cannot read the parent's request ``ContextVar``, so the scope is baked in at spawn
instead. The variable is **dedicated** (distinct from the process-wide
``PERSONA_TOOLS_SANDBOX_ROOT`` the in-process tools historically shared) precisely
so there is **no code path that can fall back to the shared flat root** (P4-D-2):

- **present** → the server roots at that pre-resolved path (the child resolves no
  identity itself — minimal trust surface);
- **absent** → the server **fails closed** (serve-and-deny, P4-D-4): every
  read/write is denied, reproducing the in-process ``None →
  SandboxViolationError`` contract across the process boundary. It never reads
  ``tools_sandbox_root`` and never mkdir's a usable shared sandbox.

The fail-closed + traversal contract is **inherited, not re-implemented** (P4-D-7):
``make_file_read_tool`` / ``make_file_write_tool`` already accept a
``SandboxRootProvider`` (``Path`` or a ``() -> Path | None`` provider) and already
deny on a ``None`` resolution — so the absent branch just hands them a
``None``-returning provider.

Exposed as ``mcp:filesystem:read_file`` + ``mcp:filesystem:write_file``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from persona.tools.builtin.file_read import make_file_read_tool
from persona.tools.builtin.file_write import make_file_write_tool

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from persona.tools._sandbox import SandboxRootProvider

__all__ = ["build"]

#: Dedicated env channel carrying the supervisor-computed, pre-resolved scoped
#: root for THIS spawn (Spec P4-D-2). Distinct from ``PERSONA_TOOLS_SANDBOX_ROOT``
#: so the absent branch has no shared-root fallback to reach for.
_SCOPE_ROOT_ENV = "PERSONA_FILESYSTEM_SCOPE_ROOT"


def build(host: str, port: int) -> FastMCP:
    """Build (do not run) the ``filesystem`` FastMCP server bound to ``host:port``.

    Roots at the per-spawn scoped path from ``PERSONA_FILESYSTEM_SCOPE_ROOT`` when
    present, ensuring the directory exists so the first read/write does not race
    on a missing directory; when absent, fails closed (serve-and-deny) without
    touching the process-wide ``tools_sandbox_root``.
    """
    from mcp.server.fastmcp import FastMCP

    scope_value = os.environ.get(_SCOPE_ROOT_ENV)
    sandbox_root: SandboxRootProvider
    if scope_value:
        scoped_root = Path(scope_value)
        # Create the scoped root up front so reads/writes resolve deterministically;
        # the path guard still rejects anything that escapes it.
        scoped_root.mkdir(parents=True, exist_ok=True)
        sandbox_root = scoped_root
    else:
        # Fail closed (P4-D-4): no scope bound for this spawn ⇒ a provider that
        # always resolves to None, so the in-process tool factory denies every
        # call. NO mkdir, NO read of the shared flat root.
        sandbox_root = lambda: None  # noqa: E731 — terse None-provider is the intent

    read_tool = make_file_read_tool(sandbox_root=sandbox_root)
    write_tool = make_file_write_tool(sandbox_root=sandbox_root)

    server = FastMCP("filesystem", host=host, port=port)

    @server.tool()
    async def read_file(path: str) -> str:
        """Read a UTF-8 text file from the persona's sandboxed workspace. Use a
        relative path like 'out/report.md'. Absolute paths and '..' escapes are
        rejected.
        """
        result = await read_tool.execute(path=path)
        if result.is_error:
            raise ValueError(result.content)
        return result.content

    @server.tool()
    async def write_file(path: str, content: str) -> str:
        """Write a UTF-8 text file into the persona's sandboxed workspace,
        creating parent directories. Use a relative path like 'out/report.md'.
        Absolute paths and '..' escapes are rejected.
        """
        result = await write_tool.execute(path=path, content=content)
        if result.is_error:
            raise ValueError(result.content)
        return result.content

    return server
