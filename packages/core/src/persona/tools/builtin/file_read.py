"""``file_read`` built-in tool — read a file from the persona's sandbox.

Path resolution goes through :func:`persona.tools._sandbox.resolve_sandbox_path`
which rejects `..`, absolute paths, NULL bytes, symlinks escaping the
sandbox, and pathological inputs (D-03-13..D-03-15).

UTF-8 with ``errors="replace"`` (D-03-17 — no chardet in v0.1). Files
larger than 1 MB are truncated and the result's ``truncated`` flag is set
(D-03-3, D-03-16). Per the T09 security-reviewer recommendation, the open
call uses ``O_NOFOLLOW`` to close the TOCTOU window between the resolver's
symlink check and this open() — a symlink swap at the final path component
between the two operations is rejected.

Failures (sandbox violation, missing file, permission error) are returned
as ``ToolResult(is_error=True, content=...)`` via the ``@tool`` decorator's
no-raise envelope. ``file_read`` does NOT emit audit events (read-only;
D-03-21).
"""

from __future__ import annotations

import os

from persona.errors import SandboxViolationError, WorkspaceLinkRefusedError
from persona.logging import get_logger
from persona.schema.tools import ToolResult
from persona.tools._sandbox import (
    SandboxRootProvider,
    open_nofollow,
    resolve_request_sandbox_root,
    resolve_sandbox_path,
)
from persona.tools.protocol import AsyncTool, tool

__all__ = ["make_file_read_tool"]

_logger = get_logger("tools.file_read")

_MAX_BYTES = 1_048_576  # 1 MB (D-03-16)


def make_file_read_tool(*, sandbox_root: SandboxRootProvider) -> AsyncTool:
    """Build the ``file_read`` :class:`AsyncTool`.

    Args:
        sandbox_root: The sandbox root SOURCE. Either a fixed
            :class:`~pathlib.Path` (CLI / tests — the explicitly chosen,
            unscoped root) OR a zero-arg provider that returns the *current
            request's* per-(owner, persona) root (the hosted path; see
            :func:`persona.tools._sandbox.resolve_request_sandbox_root`). A
            provider is re-evaluated at every dispatch, so a single cached
            toolbox stays correctly scoped across concurrent requests. A
            provider that returns ``None`` (no request scope bound) makes the
            tool fail closed — it reads NOTHING, rather than falling back to a
            shared root. The tool's path argument resolves against the resolved
            root only — no traversal escape possible.

    Returns:
        An :class:`AsyncTool` named ``file_read`` that reads UTF-8 text from
        files inside the sandbox. Failures return
        ``ToolResult(is_error=True, ...)`` — never raise.
    """

    @tool(
        name="file_read",
        description=(
            "YOU CAN read files. Use this tool whenever the user asks about a "
            "file's contents in the working directory — do not say you cannot "
            "access files: call this tool."
        ),
    )
    async def file_read(path: str) -> ToolResult:
        try:
            root = resolve_request_sandbox_root(sandbox_root)
            resolved = resolve_sandbox_path(root, path)
        except SandboxViolationError as e:
            _logger.warning("file_read sandbox violation", requested=path, reason=str(e))
            return ToolResult(
                tool_name="file_read",
                content=f"SandboxViolationError: {e}",
                is_error=True,
            )

        # O_NOFOLLOW closes the TOCTOU window between resolver's symlink check
        # and this open() — a swap of the final path component to a symlink
        # between the two operations is rejected (security review T09). Via the
        # shared sandbox opener (R2-D-4). On Windows the opener refuses a link in
        # ANY component below ``root`` (Spec WIN, T1), so the root is passed.
        try:
            fd = open_nofollow(resolved, os.O_RDONLY, root=root)
        except FileNotFoundError:
            return ToolResult(
                tool_name="file_read",
                content=f"FileNotFoundError: {path}",
                is_error=True,
            )
        except IsADirectoryError:
            return ToolResult(
                tool_name="file_read",
                content=f"IsADirectoryError: {path} is a directory, not a file",
                is_error=True,
            )
        except WorkspaceLinkRefusedError as e:
            # Windows: a link or shortcut in the path, or a hard-linked file (Spec WIN,
            # T1). Its message is human and carries no host path; the context (which
            # does) stays out of the model's view.
            return ToolResult(
                tool_name="file_read",
                content=f"WorkspaceLinkRefusedError: {e.message}",
                is_error=True,
            )
        except PermissionError as e:
            return ToolResult(
                tool_name="file_read",
                content=f"PermissionError: {e}",
                is_error=True,
            )
        except OSError as e:
            # ELOOP from O_NOFOLLOW on a symlink; also covers other OS errors.
            return ToolResult(
                tool_name="file_read",
                content=f"OSError: {e}",
                is_error=True,
            )

        try:
            raw = os.read(fd, _MAX_BYTES + 1)  # read one extra byte to detect overflow
        finally:
            os.close(fd)

        truncated = len(raw) > _MAX_BYTES
        if truncated:
            raw = raw[:_MAX_BYTES]
        text = raw.decode("utf-8", errors="replace")

        return ToolResult(
            tool_name="file_read",
            content=text,
            truncated=truncated,
            data={
                "path": path,
                "bytes_read": str(len(raw)),
                "encoding": "utf-8",
            },
        )

    return file_read
