"""``file_write`` built-in tool — write a file to the persona's sandbox.

Creates parent directories inside the sandbox as needed. Overwrites
existing files (per spec §6.4). UTF-8 only (D-03-17).

Per Phase 1 refinement #6 + D-03-21 + D-03-25, emits one
:class:`ToolAuditEvent` per write (``action="write"``) when a
``ToolAuditLogger`` is injected at construction. The audit fires AFTER
the write succeeds (failed writes don't pollute the audit log; failures
are logged at WARNING via :mod:`persona.logging` instead).

Path resolution goes through :func:`persona.tools._sandbox.resolve_sandbox_path`.
The final open uses ``O_NOFOLLOW | O_CREAT | O_WRONLY | O_TRUNC`` to close
the TOCTOU window between the resolver's symlink check and the actual
write — a symlink swap of the final path component is rejected.
"""

from __future__ import annotations

import mimetypes
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.errors import SandboxViolationError, WorkspaceLinkRefusedError
from persona.logging import get_logger
from persona.schema.tools import PersistedArtifact, ToolResult
from persona.tools._sandbox import (
    SandboxRootProvider,
    make_dirs_nofollow,
    open_nofollow,
    resolve_request_sandbox_root,
    resolve_sandbox_path,
)
from persona.tools.audit import ToolAuditEvent, ToolAuditLogger
from persona.tools.protocol import AsyncTool, tool

if TYPE_CHECKING:
    from persona.tools.workspace_persister import WorkspacePersister

__all__ = ["make_file_write_tool"]

_logger = get_logger("tools.file_write")


def make_file_write_tool(
    *,
    sandbox_root: SandboxRootProvider,
    audit_logger: ToolAuditLogger | None = None,
    persona_id: str | None = None,
    persister: WorkspacePersister | None = None,
) -> AsyncTool:
    """Build the ``file_write`` :class:`AsyncTool`.

    Args:
        sandbox_root: The sandbox root SOURCE. Either a fixed
            :class:`~pathlib.Path` (CLI / tests — the explicitly chosen,
            unscoped root) OR a zero-arg provider that returns the *current
            request's* per-(owner, persona) root (the hosted path; see
            :func:`persona.tools._sandbox.resolve_request_sandbox_root`). A
            provider is re-evaluated at every dispatch, so a single cached
            toolbox stays correctly scoped across concurrent requests. A
            provider that returns ``None`` (no request scope bound) makes the
            tool fail closed — it writes NOTHING. The tool's path argument
            resolves against the resolved root only.
        audit_logger: Optional tool-audit sink. If provided, every
            successful write emits one :class:`ToolAuditEvent` with
            ``action="write"`` per D-03-21.
        persona_id: Persona identifier for audit records. ``None`` for CLI
            development; audit lines then route to ``_cli.tools.jsonl``.
        persister: Optional :class:`WorkspacePersister` (Spec 28). When
            provided, the written bytes are mirrored to the persona
            workspace and the resolved :class:`PersistedArtifact` lands on
            :attr:`ToolResult.artifacts` so the chat UI renders a file card.
            When ``None`` the result is byte-identical to the pre-Spec-28
            shape (empty ``artifacts``). A persist failure surfaces as a
            structured ``ToolResult(is_error=True)`` (the file is already
            written to the sandbox; only the workspace mirror failed).

    Returns:
        An :class:`AsyncTool` named ``file_write`` that creates/overwrites
        files inside the sandbox. Failures return
        ``ToolResult(is_error=True, ...)`` — never raise.
    """

    @tool(
        name="file_write",
        description=(
            "YOU CAN write files. Use this tool whenever the user asks you to "
            "save, export, or create a file — do not say you cannot write "
            "files: call this tool. Writes content to a file in the working "
            "directory (use a relative path like 'out/report.md'), creating it "
            "if absent and overwriting if present."
        ),
    )
    async def file_write(path: str, content: str) -> ToolResult:
        try:
            root = resolve_request_sandbox_root(sandbox_root)
            resolved = resolve_sandbox_path(root, path)
        except SandboxViolationError as e:
            _logger.warning("file_write sandbox violation", requested=path, reason=str(e))
            return ToolResult(
                tool_name="file_write",
                content=f"SandboxViolationError: {e}",
                is_error=True,
            )

        # Create parent directories inside the sandbox (still within root after
        # resolver verified). mkdir is idempotent; we do not error on existing dirs.
        # On Windows each folder is created with every link below ``root`` refused
        # (Spec WIN, T1); on POSIX this is the same ``mkdir(parents=True)`` as before.
        try:
            make_dirs_nofollow(resolved.parent, root=root)
        except WorkspaceLinkRefusedError as e:
            # Windows: a link or shortcut in the path, or a hard-linked file (Spec WIN,
            # T1). Its message is human and carries no host path.
            return ToolResult(
                tool_name="file_write",
                content=f"WorkspaceLinkRefusedError: {e.message}",
                is_error=True,
            )
        except OSError as e:
            return ToolResult(
                tool_name="file_write",
                content=f"OSError creating parent directories: {e}",
                is_error=True,
            )

        # Lone surrogates (D800-DFFF) in `content` cause UnicodeEncodeError.
        # LLMs occasionally produce these on malformed JSON; catch explicitly so
        # the model sees an informative error rather than an opaque envelope.
        # (Security review Finding 5, HIGH.)
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as e:
            return ToolResult(
                tool_name="file_write",
                content=(
                    "UnicodeEncodeError: content contains characters that "
                    f"cannot be encoded as UTF-8: {e}"
                ),
                is_error=True,
            )

        # O_NOFOLLOW closes the TOCTOU window between resolver check and open,
        # via the shared sandbox opener (R2-D-4). O_CREAT|O_TRUNC overwrites
        # existing files per spec §6.4. File mode 0o600 is correct for v0.1
        # single-user CLI; the hosted path (spec 08) uses Postgres-backed storage
        # and does not produce local files.
        try:
            fd = open_nofollow(
                resolved,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
                root=root,
            )
        except WorkspaceLinkRefusedError as e:
            # Windows: a link or shortcut in the path, or a hard-linked file (Spec WIN,
            # T1). Its message is human and carries no host path.
            return ToolResult(
                tool_name="file_write",
                content=f"WorkspaceLinkRefusedError: {e.message}",
                is_error=True,
            )
        except PermissionError as e:
            return ToolResult(
                tool_name="file_write",
                content=f"PermissionError: {e}",
                is_error=True,
            )
        except OSError as e:
            # ELOOP from O_NOFOLLOW when path is a symlink; also other OS errors.
            return ToolResult(
                tool_name="file_write",
                content=f"OSError: {e}",
                is_error=True,
            )

        # os.write on a regular file is all-or-nothing per POSIX (no looping
        # required), but it can still raise OSError (ENOSPC, EIO, ...) which
        # must not escape the @tool envelope opaquely. (Security review Finding 10.2.)
        try:
            try:
                os.write(fd, encoded)
            except OSError as e:
                return ToolResult(
                    tool_name="file_write",
                    content=f"OSError writing file: {e}",
                    is_error=True,
                )
        finally:
            os.close(fd)

        # Audit the successful write (D-03-21, D-03-25). Failed writes already
        # returned above.
        if audit_logger is not None:
            audit_logger.emit(
                ToolAuditEvent(
                    timestamp=datetime.now(UTC),
                    persona_id=persona_id,
                    tool_name="file_write",
                    action="write",
                    resource=path,
                    metadata={"bytes": str(len(encoded))},
                )
            )

        # Populate produced_files for parity with sandbox/tool.py's code_execution
        # result shape, so downstream consumers (Spec 19 file-output surfaces) can
        # treat both code-execution outputs and direct writes uniformly.
        media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        produced_files = [
            {
                "path": path,
                "size_bytes": str(len(encoded)),
                "media_type": media_type,
            }
        ]

        # Spec 28 — mirror the written bytes into the persona workspace when a
        # persister is injected, so the chat UI can render an inline file card.
        # None ⇒ pre-Spec-28 shape (empty artifacts). The sandbox file is
        # already written; a workspace-mirror failure surfaces structured.
        artifacts: tuple[PersistedArtifact, ...] = ()
        if persister is not None:
            try:
                artifact = await persister.persist(
                    encoded,
                    mime_type=media_type,
                    suggested_filename=path.rsplit("/", 1)[-1],
                )
                # Images render inline above the card; other files = card only.
                if media_type.startswith("image/"):
                    artifact = artifact.model_copy(update={"rendered_inline": True})
                artifacts = (artifact,)
            except Exception as e:  # noqa: BLE001 — any persist failure → structured result
                _logger.warning("file_write workspace mirror failed", path=path, reason=str(e))
                return ToolResult(
                    tool_name="file_write",
                    content=f"persist_failed: wrote to sandbox but workspace mirror failed: {e}",
                    is_error=True,
                    data={"path": path, "bytes_written": str(len(encoded))},
                )

        return ToolResult(
            tool_name="file_write",
            content=f"Wrote {len(encoded)} bytes to {path}",
            data={
                "path": path,
                "bytes_written": str(len(encoded)),
                "produced_files": produced_files,
            },
            artifacts=artifacts,
        )

    return file_write
