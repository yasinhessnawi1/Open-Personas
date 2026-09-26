"""Boundary-crossing types for the code-execution sandbox (spec 12 T01).

Per **D-12-14**, these are Pydantic v2 frozen models with ``extra="forbid"`` —
NOT the spec §4.1 ``@dataclass(frozen=True, slots=True)`` sketches. Phase-1
convention re-applied (D-01-12 / D-02-2 / D-03-3 / D-04-1 / D-05-9 / D-06-1).

The types cross four boundaries:

1. Returned from the ``code_execution`` tool as ``ToolResult.data`` (T03).
2. Serialized into the conversation via ``format_tool_result``.
3. Audited via the ``ToolAuditLogger`` port (D-12-8).
4. (For the hosted path) cross the SSE / API surface.

Container collection fields are :class:`tuple` rather than :class:`list` — a
list inside a frozen model can still mutate its contents; tuples cannot.

D-12-4 reminder: ``NetworkPolicy`` is constructed by the tool factory from
the persona's YAML, NOT passed by the model in the tool call. The substrate
egress filter (R-12-5) still blocks metadata + RFC-1918 ranges regardless
of ``allowed_hosts``.
"""

from __future__ import annotations

import mimetypes
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_MEDIA_TYPE",
    "ExecutionOutcome",
    "ExecutionResult",
    "RefusedFile",
    "NetworkPolicy",
    "ResourceLimits",
    "SandboxFile",
    "guess_media_type",
    "produced_file_path_violation",
    "refused_file",
]

#: Fallback media type for files whose extension does not map to a known type.
#: The frontend renderer + the GET serve route both treat this as "binary,
#: not inline" — so a file with this type lands as a download card, never an
#: inline image (the safe default).
DEFAULT_MEDIA_TYPE = "application/octet-stream"


def guess_media_type(path: str) -> str:
    """Infer a produced file's media type from its path extension.

    The substrate (Docker or E2B) reports a file's *bytes*, not its IANA media
    type — so both backends infer the type from the path so the unified
    ``ToolResult.artifacts`` render path can decide inline-vs-download (the
    frontend keys inline-image rendering on ``media_type.startswith("image/")``,
    and there is no extension fallback on the wire). Without inference every
    produced file is :data:`DEFAULT_MEDIA_TYPE` and a produced PNG never renders
    inline (D-12-X-produced-media-type).

    Uses the stdlib :func:`mimetypes.guess_type` table (covers the image,
    document, and text formats the renderers support) and falls back to
    :data:`DEFAULT_MEDIA_TYPE` for unknown / extension-less paths so the
    contract is always a well-formed media type.

    Args:
        path: A file path or basename (relative or absolute). Only the
            extension is consulted; no I/O is performed.

    Returns:
        An IANA media type string (e.g. ``"image/png"``) or
        :data:`DEFAULT_MEDIA_TYPE` when the extension is unknown.
    """
    guessed, _encoding = mimetypes.guess_type(path)
    return guessed or DEFAULT_MEDIA_TYPE


ExecutionOutcome = Literal["ok", "error", "timeout", "oom", "killed"]
"""Discriminator for :class:`ExecutionResult`.

- ``"ok"`` — code returned exit status 0.
- ``"error"`` — code raised / returned non-zero within limits.
- ``"timeout"`` — wall-clock cap hit; substrate killed the process.
- ``"oom"`` — memory cap hit; substrate's OOM killer fired.
- ``"killed"`` — any other forced kill (pids-limit, cancel, host shutdown).
"""


class ResourceLimits(BaseModel):
    """Per-execution resource caps enforced by the substrate, not by the code.

    Limits live **one layer below** the model-generated code so the code itself
    cannot raise them. Spec §4.2 "Limits are explicit and enforced by the
    substrate". Defaults are conservative.

    Attributes:
        cpu_cores: CPU allocation. Fractional values supported by both Docker
            (``--cpus``) and E2B (``cpu_count``).
        memory_mb: Memory cap in MiB. Substrate OOM-killer fires above.
        wall_clock_s: Hard timeout in seconds. Substrate kills above.
        disk_mb: Workspace disk quota in MiB.
        max_stdout_bytes: Cap on stdout returned to the conversation. Beyond
            this, output is truncated with an explicit marker — never dropped
            silently (spec-02/04/06 fail-safe-truncation pattern).
        max_produced_files: Cap on the count of produced files reported back.
        max_produced_file_mb: Per-file size cap on produced files.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_cores: float = Field(default=1.0, gt=0.0)
    memory_mb: int = Field(default=512, gt=0)
    wall_clock_s: float = Field(default=30.0, gt=0.0)
    disk_mb: int = Field(default=256, gt=0)
    max_stdout_bytes: int = Field(default=64_000, gt=0)
    max_produced_files: int = Field(default=20, ge=0)
    max_produced_file_mb: int = Field(default=50, gt=0)


class NetworkPolicy(BaseModel):
    """Per-sandbox network egress policy.

    Network is **OFF by default** (the safe default per spec §4.2). When
    enabled, only hosts in ``allowed_hosts`` may be reached — and substrate
    egress rules (R-12-5: ``169.254.0.0/16``, RFC-1918, IPv6 link-local, etc.)
    still block those ranges **regardless** of the allow-list.

    Constructed by the tool factory from the persona's YAML (D-12-4) — never
    passed by the model in the tool call.

    Attributes:
        enabled: Whether egress is permitted at all.
        allowed_hosts: Hosts to allow when ``enabled=True``. The substrate
            egress filter still blocks metadata + private ranges first.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    allowed_hosts: tuple[str, ...] = Field(default_factory=tuple)


class SandboxFile(BaseModel):
    """A file referenced by or produced by the sandbox.

    Used in two directions:

    - **Into the sandbox:** ``content_bytes`` is the payload to write at
      ``path`` inside the workspace input area.
    - **Out of the sandbox:** ``content_bytes`` may be populated (small files
      inlined) or ``None`` (large files referenced only — caller reads from
      the workspace by ``path``).

    Attributes:
        path: Path relative to the workspace root. Never absolute.
        content_bytes: File payload, or ``None`` when only referenced.
        size_bytes: Size in bytes.
        media_type: MIME-style media type; default ``application/octet-stream``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    content_bytes: bytes | None = None
    size_bytes: int = Field(default=0, ge=0)
    media_type: str = "application/octet-stream"


class RefusedFile(BaseModel):
    """A produced file the workspace refused to take, and the human reason (Spec WIN, T1.5).

    Attributes:
        path: A display-safe preview of the file's name (control characters removed,
            truncated); never used as a path.
        reason: A short human explanation, shown to the model so it can rename.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    reason: str


#: The longest name preview a :class:`RefusedFile` carries.
_REFUSED_PREVIEW_CHARS = 120


def refused_file(path: str, reason: str) -> RefusedFile:
    """Build a :class:`RefusedFile` whose ``path`` is safe to show (no control characters)."""
    shown = "".join(c for c in path if ord(c) >= 32 and ord(c) != 127)
    if len(shown) > _REFUSED_PREVIEW_CHARS:
        shown = shown[:_REFUSED_PREVIEW_CHARS] + "..."
    return RefusedFile(path=shown, reason=reason)


def produced_file_path_violation(path: str) -> str | None:
    """Why a produced file's relative path must be refused, on every platform, or ``None``.

    Code in the sandbox can create any name its Linux filesystem allows, and those
    names are later joined onto a host folder. A backslash or a colon is an ordinary
    character on Linux but a separator, a drive (``C:``) or an alternate data stream
    (``name:stream``) on Windows; ``..`` and a leading ``/`` climb or jump out of the
    destination. Such a file is refused at discovery, before any host path is built.

    Args:
        path: The produced file's path relative to the sandbox output folder.

    Returns:
        A short human reason, or ``None`` when the path is acceptable.
    """
    if not path:
        return "the name is empty"
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        return "the name contains control characters"
    if "\\" in path:
        return "the name contains a backslash"
    if ":" in path:
        return "the name contains a colon"
    if path.startswith("/"):
        return "the path is absolute"
    if ".." in path.split("/"):
        return "the path climbs out of its folder with '..'"
    return None


class ExecutionResult(BaseModel):
    """The result of one :meth:`CodeSandbox.execute` call.

    Fed back into the conversation as a :class:`ToolResult` by the
    ``code_execution`` tool factory (T03). The model reads ``stdout`` /
    ``stderr`` / ``outcome``; the runtime persists ``produced_files`` to the
    workspace; the audit logger records ``duration_ms`` + ``outcome`` (D-12-8).

    Attributes:
        stdout: Captured standard output. May be truncated (see
            ``truncated_stdout``); never dropped silently.
        stderr: Captured standard error. ANSI codes stripped; absolute
            container paths sanitized by the backend before this point.
        exit_status: Process exit status. 0 on success; non-zero on error;
            -1 when the substrate killed the process before it could exit.
        outcome: Discriminator across ``ok`` / ``error`` / ``timeout`` /
            ``oom`` / ``killed``.
        produced_files: Files the code wrote to the workspace output area.
        duration_ms: Wall-clock duration in milliseconds.
        truncated_stdout: ``True`` when stdout exceeded ``max_stdout_bytes``
            and was truncated with an explicit marker.
        truncated_files: ``True`` when produced files exceeded the count or
            size caps in ``ResourceLimits``.
        refused_files: Produced files the backend refused at discovery because
            their path is not safe to bring into the workspace (see
            :func:`produced_file_path_violation`); the tool tells the model why.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stdout: str
    stderr: str
    exit_status: int
    outcome: ExecutionOutcome
    produced_files: tuple[SandboxFile, ...] = Field(default_factory=tuple)
    duration_ms: float = Field(default=0.0, ge=0.0)
    truncated_stdout: bool = False
    truncated_files: bool = False
    refused_files: tuple[RefusedFile, ...] = Field(default_factory=tuple)
