"""Sandbox path resolver for the file_read / file_write built-ins (T09).

Pure function. No I/O, no ``os.access``, no read/write. The caller (file
tools in T10) opens the resolved path; we only verify the path *would* be
inside the sandbox root after symlink resolution.

Validation order (research.md §5.2 — D-03-13, D-03-14, D-03-15):
1. NULL byte → reject (stdlib raises ``ValueError`` from .resolve() — we
   want our own domain error before the bytes touch ``Path``).
2. Length cap > 4096 chars → reject (stdlib doesn't bound; DOS protection).
3. Backslash on POSIX (``os.sep == "/"``) → reject (defense in depth;
   ``a\\b\\c`` is a single weird filename on POSIX, operator-confusing).
4. Empty / whitespace-only → reject (file tools never want "the directory
   itself"; D-03-13).
5. Absolute path (``os.path.isabs``) → reject.
6. ``Path.resolve(strict=False)`` + ``is_relative_to(root.resolve())`` →
   the inner symlink/traversal check (D-03-14).

Returns the resolved absolute ``Path`` (which may not yet exist —
``file_write`` creates new files). Callers are responsible for opening it.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from persona.errors import SandboxViolationError

#: A sandbox-root source for the file tools. Either a fixed ``Path`` (CLI /
#: tests — the unscoped, explicitly-chosen root) or a zero-arg provider that
#: returns the *current request's* per-(owner, persona) root (the hosted path).
#: A provider that returns ``None`` means "no request scope is bound" and the
#: file tools MUST fail closed (deny) rather than fall back to any shared root —
#: this is the cross-context isolation guarantee. See
#: :func:`resolve_request_sandbox_root`.
SandboxRootProvider = Path | Callable[[], Path | None]

__all__ = [
    "SandboxRootProvider",
    "is_regular_file_nofollow",
    "open_nofollow",
    "read_nofollow_bytes",
    "resolve_request_sandbox_root",
    "resolve_sandbox_path",
    "write_nofollow_bytes",
]

# Default mode for a freshly created sandbox file: owner read/write only. Matches
# the single-user CLI posture of file_write / image_service (spec 03 §6.4).
_NEW_FILE_MODE = 0o600


def open_nofollow(path: Path, flags: int, mode: int = _NEW_FILE_MODE) -> int:
    """Open ``path`` with ``O_NOFOLLOW`` and return the file descriptor (Spec R2, F-03).

    The single hardened opener shared by every sandbox read/write/serve site
    (R2-D-4). ``O_NOFOLLOW`` closes the narrow TOCTOU window between
    :func:`resolve_sandbox_path`'s symlink check and the actual ``open()``: if the
    final path component is (or is swapped to) a symlink, the open fails with
    ``ELOOP`` rather than following the link out of the sandbox. Used on **every**
    platform (R2-D-5); ``openat2(RESOLVE_NO_SYMLINKS)`` — which would additionally
    reject symlinks in *intermediate* directories — is an unbuilt Linux-only future
    hardening (R2-R-1), not in stdlib.

    Args:
        path: The already-resolved (sandbox-validated) path to open.
        flags: ``os.open`` flags (e.g. ``os.O_RDONLY`` or
            ``os.O_WRONLY | os.O_CREAT | os.O_TRUNC``). ``O_NOFOLLOW`` is added.
        mode: Permission bits for a newly created file (default ``0o600``).

    Returns:
        The open file descriptor. The caller owns it and MUST close it.

    Raises:
        OSError: ``ELOOP`` when ``path``'s final component is a symlink; plus the
            usual ``FileNotFoundError`` / ``IsADirectoryError`` / ``PermissionError``
            / other ``OSError`` cases the caller maps to its domain response.
    """
    return os.open(path, flags | os.O_NOFOLLOW, mode)


def is_regular_file_nofollow(path: Path) -> bool:
    """Whether ``path`` is a regular file, WITHOUT following a final symlink.

    The serve/delete sites check ``Path.is_file()`` to decide a target exists —
    but ``is_file()`` *follows* a trailing symlink, so a link swapped into the
    final component after :func:`resolve_sandbox_path` would report ``True`` for an
    out-of-sandbox target (a confused-deputy). This uses ``os.lstat`` so a
    symlink (or any non-regular entry) reads as ``False``; a missing path is
    ``False`` (mirrors ``Path.is_file()``'s missing-ok semantics).
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def read_nofollow_bytes(path: Path) -> bytes:
    """Read all bytes from ``path`` via :func:`open_nofollow` (symlink-swap safe).

    The serve/read counterpart used by the image/document/artifact download sites.
    Raises ``OSError`` (``ELOOP``) if the final component is a symlink.
    """
    fd = open_nofollow(path, os.O_RDONLY)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1 << 20)  # 1 MiB at a time
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_nofollow_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via :func:`open_nofollow` (symlink-swap safe).

    The mirror/stage counterpart. ``O_CREAT | O_TRUNC`` overwrites an existing
    regular file; a swapped-in symlink as the final component is rejected with
    ``OSError`` (``ELOOP``) rather than clobbering its out-of-sandbox target.
    """
    fd = open_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


_MAX_PATH_LENGTH = 4096


def resolve_request_sandbox_root(source: SandboxRootProvider) -> Path:
    """Resolve the per-call sandbox root from a fixed Path or a request provider.

    Args:
        source: Either a fixed :class:`~pathlib.Path` (CLI / tests — the
            explicitly chosen, unscoped root) or a zero-arg callable that
            returns the current request's per-(owner, persona) root (the hosted
            path). The callable is invoked at *dispatch time*, so a single
            cached toolbox stays correctly scoped across concurrent requests.

    Returns:
        The sandbox root :class:`~pathlib.Path` to resolve tool paths against.

    Raises:
        SandboxViolationError: When ``source`` is a provider that returns
            ``None`` — no request scope is bound. The file tools translate this
            into a structured ``ToolResult(is_error=True, ...)`` and read /
            write NOTHING. We never fall back to a shared root (fail closed).
    """
    if callable(source):
        root = source()
        if root is None:
            raise SandboxViolationError(
                _violation_message("no request scope bound for file access", "no_scope"),
                context={"reason": "no_scope"},
            )
        return root
    return source


# Per-reason recovery guidance appended to every SandboxViolationError message
# (R9-007, refining T10 / D-25-5 / spec §2.5). The model that triggered the
# violation reads the ToolResult text and, without an ACTIONABLE fix keyed to
# *why* the path was rejected, tends to retry the same escaping shape — burning
# steps and model spend (owner saw dozens of ``file_write sandbox violation``
# warnings in one run). A single generic "use out/report.md" hint fired for
# every reason and did not tell the model, e.g., to swap backslashes for
# forward slashes or to name a file instead of the directory. Each entry below
# is a terse, imperative, model-facing correction that names the concrete valid
# path form for THAT failure. The reason discriminator is emitted separately by
# :func:`_violation_message` (``[reason=<reason>]``) and left in ``context`` so
# audits/tests that key on the reason keep working. This is the ONE place the
# hint text lives — both ``file_read`` and ``file_write`` surface it via
# ``str(exc)``, so there is no per-tool copy to drift.
_CORRECTION_BY_REASON: dict[str, str] = {
    "absolute": (
        "Paths must be RELATIVE to the working directory and stay inside it — "
        "e.g. 'out/report.md'. Do not use an absolute path (a leading '/') or '..'."
    ),
    "escape": (
        "Paths must be RELATIVE to the working directory and stay inside it — "
        "e.g. 'out/report.md'. Do not use '..' or any path that climbs out of "
        "the working directory."
    ),
    "root_reference": (
        "Provide a filename inside the working directory, e.g. 'notes.md', not "
        "the directory itself."
    ),
    "mixed_separators": (
        "Use forward slashes '/' to separate path segments, e.g. 'out/report.md'; "
        "backslashes are not allowed."
    ),
    "too_long": (
        "Shorten the path to at most 4096 characters; use a short relative path "
        "like 'out/report.md'."
    ),
    "null_byte": (
        "Remove control/NUL characters from the path; use a plain relative path "
        "like 'out/report.md'."
    ),
    "empty": ("Provide a non-empty relative filename, e.g. 'out/report.md'."),
    "no_scope": (
        "No workspace is bound for this request, so file access is unavailable "
        "right now — changing the path will not help; do not retry."
    ),
}

# Fallback for any reason not explicitly mapped, keeping the resolver total and
# never leaving the model with a bare "no". Points at the same relative form.
_DEFAULT_CORRECTION = "Use a relative path inside the working directory, e.g. 'out/report.md'."


def _correction_for(reason: str) -> str:
    """Return terse, model-facing recovery guidance keyed to a violation reason.

    Args:
        reason: The machine discriminator set in ``context["reason"]`` (one of
            the keys of :data:`_CORRECTION_BY_REASON`).

    Returns:
        An imperative one-liner telling the model the concrete valid path form
        for that specific failure, so it can recover instead of retrying the
        same rejected shape. Unmapped reasons fall back to
        :data:`_DEFAULT_CORRECTION`.
    """
    return _CORRECTION_BY_REASON.get(reason, _DEFAULT_CORRECTION)


def _violation_message(summary: str, reason: str) -> str:
    """Compose a model-recoverable SandboxViolationError message.

    Args:
        summary: Human-readable statement of what was wrong (no trailing
            punctuation).
        reason: The machine discriminator (mirrors ``context["reason"]``) so
            the model sees the same token in the prose that it does in the
            structured context.

    Returns:
        ``"<summary> [reason=<reason>]; <reason-keyed correction>"`` — a string
        that tells the model both what failed and, keyed to *why*, a concrete
        path form that would succeed, enabling a recovery retry instead of a
        repeated escaping attempt.
    """
    return f"{summary} [reason={reason}]; {_correction_for(reason)}"


def resolve_sandbox_path(root: Path, requested: str) -> Path:
    """Resolve ``requested`` against ``root``; reject any escape attempts.

    Args:
        root: Sandbox root. Must exist or be createable by the caller;
            we resolve it via :meth:`Path.resolve` to canonicalise.
        requested: Caller-supplied path (from a tool argument). May be
            absolute, contain ``..``, NULL bytes, etc. — all rejected.

    Returns:
        An absolute :class:`Path` guaranteed to be inside ``root.resolve()``.
        The path may not yet exist (``file_write`` creates new files).

    Raises:
        SandboxViolationError: If the requested path escapes the sandbox
            or fails any of the validation checks listed in the module
            docstring. The exception's ``context`` carries the offending
            input (control-char-stripped and truncated) and a ``reason``
            discriminator.

    Note:
        Callers should open the returned path with ``os.O_NOFOLLOW`` (or
        ``open(..., opener=...)`` wrapping ``os.open`` with that flag) to
        close the narrow TOCTOU window between this resolver's symlink
        check and the actual file open. See spec-03 decision D-03-14 for
        the full risk-acceptance rationale (single-tenant CLI scope in v0.1;
        post-September multi-tenant hardening is spec 11).
    """
    # Pre-Path validation (cheap; runs first so the stdlib doesn't bite us).

    if "\x00" in requested:
        raise SandboxViolationError(
            _violation_message("null byte in path", "null_byte"),
            context={"reason": "null_byte", "requested_preview": _preview(requested)},
        )

    if len(requested) > _MAX_PATH_LENGTH:
        raise SandboxViolationError(
            _violation_message("path too long", "too_long"),
            context={"reason": "too_long", "length": str(len(requested))},
        )

    if os.sep == "/" and "\\" in requested:
        raise SandboxViolationError(
            _violation_message("windows-style separator on POSIX", "mixed_separators"),
            context={"reason": "mixed_separators", "requested": _preview(requested)},
        )

    if not requested or requested.strip() == "":
        raise SandboxViolationError(
            _violation_message("empty path", "empty"),
            context={"reason": "empty"},
        )

    # PurePosixPath gives deterministic behavior regardless of host separator;
    # we already rejected backslash on POSIX above.
    if PurePosixPath(requested).is_absolute():
        raise SandboxViolationError(
            _violation_message("absolute path not allowed", "absolute"),
            context={"reason": "absolute", "requested": _preview(requested)},
        )

    # Reject paths that resolve to the sandbox root itself (D-03-13 spirit:
    # file_read/file_write never want "the directory itself" as a target).
    # PurePosixPath normalizes "." and "./" to a path with empty parts;
    # we also catch "." with surrounding whitespace explicitly.
    pure = PurePosixPath(requested)
    if pure.parts == () or pure.parts == (".",) or requested.strip() in (".", "./"):
        raise SandboxViolationError(
            _violation_message("path resolves to sandbox root directory", "root_reference"),
            context={"reason": "root_reference", "requested": _preview(requested)},
        )

    # Path + symlink resolution. strict=False because the caller's target
    # may not exist yet (file_write creates new files).
    root_resolved = root.resolve(strict=False)
    candidate = (root / requested).resolve(strict=False)

    if not candidate.is_relative_to(root_resolved):
        raise SandboxViolationError(
            _violation_message("path escapes sandbox", "escape"),
            context={
                "reason": "escape",
                "requested": _preview(requested),
                "resolved": _preview(str(candidate)),
            },
        )

    return candidate


def _preview(s: str, *, max_len: int = 120) -> str:
    """Make a user-supplied string safe for inclusion in audit-log context.

    Strips ASCII control characters (notably NUL, which would survive into
    JSONL audit lines as a literal ``\\u0000`` and break some downstream
    log shippers / SIEM parsers). Preserves printable Unicode (including
    non-Latin scripts) and TAB. Truncates to ``max_len`` characters.
    """
    cleaned_chars = [c for c in s if c == "\t" or ord(c) >= 32]
    cleaned = "".join(cleaned_chars)
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[:max_len] + "...<truncated>"
