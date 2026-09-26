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

On Windows (Spec WIN, T1.3/T1.5) two things differ. The Windows lexical rules in
:mod:`persona.tools._winpath` run after step 4, before any filesystem call (backslash,
drive, reserved characters and device names, trailing dot or space). Step 6 uses
:mod:`persona.tools._winresolve` instead of ``resolve()``: it normalises lexically,
walks the existing components by handle and stops at the first link below the root,
refusing it as ``link`` (target inside) or ``escape`` (target outside, a network share
or a device) without ever following it or contacting anyone. The no-follow opener
still refuses every link below the root when the path is opened.

Returns the resolved absolute ``Path`` (which may not yet exist —
``file_write`` creates new files). Callers are responsible for opening it.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath

from persona.errors import SandboxViolationError
from persona.tools._winpath import windows_path_violation

if sys.platform == "win32":
    # Windows has no O_NOFOLLOW; its no-follow opener lives in a Windows-only module
    # that is never imported on POSIX (Spec WIN, T1).
    from persona.tools import _winopen
    from persona.tools._winresolve import resolve_without_following

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
    "delete_regular_file_nofollow",
    "is_regular_file_nofollow",
    "iter_regular_files_nofollow",
    "make_dirs_nofollow",
    "open_nofollow",
    "read_file_under_root",
    "read_nofollow_bytes",
    "resolve_request_sandbox_root",
    "resolve_sandbox_path",
    "write_file_under_root",
    "write_nofollow_bytes",
]

# Default mode for a freshly created sandbox file: owner read/write only. Matches
# the single-user CLI posture of file_write / image_service (spec 03 §6.4).
_NEW_FILE_MODE = 0o600


def _windows_root(path: Path, root: Path | None, final_component_only: bool) -> Path:
    """The folder the Windows opener works under (Spec WIN, T1.5, fail secure).

    The caller's ``root`` is normal: a link in any component below it is refused.
    Without a root the call is refused (``ValueError``), unless the caller opts in with
    ``final_component_only=True``: then the file's own folder is the root, which gives
    ONLY the final-component guarantee (the one POSIX ``O_NOFOLLOW`` gives), because
    links in the folders above the file are followed. Nothing gets that weaker
    guarantee by forgetting an argument.
    """
    if root is not None:
        return root.resolve(strict=False)
    if not final_component_only:
        msg = (
            "the Windows no-follow opener needs the workspace root (root=...); pass "
            "final_component_only=True to accept the final-component guarantee only"
        )
        raise ValueError(msg)
    return Path(path).absolute().parent


def open_nofollow(
    path: Path,
    flags: int,
    mode: int = _NEW_FILE_MODE,
    *,
    root: Path | None = None,
    final_component_only: bool = False,
) -> int:
    """Open ``path`` without following links and return the file descriptor (Spec R2, F-03).

    The single hardened opener shared by every sandbox read/write/serve site
    (R2-D-4).

    **POSIX** (unchanged): ``O_NOFOLLOW`` closes the narrow TOCTOU window between
    :func:`resolve_sandbox_path`'s symlink check and the actual ``open()``: if the
    final path component is (or is swapped to) a symlink, the open fails with
    ``ELOOP`` rather than following the link out of the sandbox.
    ``openat2(RESOLVE_NO_SYMLINKS)``, which would additionally reject symlinks in
    *intermediate* directories, is an unbuilt Linux-only future hardening (R2-R-1).
    ``root`` is ignored on POSIX.

    **Windows** (Spec WIN, T1): there is no ``O_NOFOLLOW``, so the open goes through
    :mod:`persona.tools._winopen`, relative to a handle on ``root`` with every reparse
    point refused (symbolic link, junction or any other, final OR intermediate) and a
    hard-linked file refused; the descriptor is binary, and a created file gets an
    owner-only DACL (the Windows reading of ``mode=0o600``; ``mode`` is otherwise
    unused there). Without ``root`` the call is refused on Windows unless
    ``final_component_only=True`` opts in to the final-component guarantee only
    (the file's own folder becomes the root); production callers pass the workspace
    root, and a source guard enforces it.

    Args:
        path: The already-resolved (sandbox-validated) path to open.
        flags: ``os.open`` flags (e.g. ``os.O_RDONLY`` or
            ``os.O_WRONLY | os.O_CREAT | os.O_TRUNC``). ``O_NOFOLLOW`` is added on POSIX.
        mode: Permission bits for a newly created file (default ``0o600``).
        root: The workspace root ``path`` was resolved under (keyword-only).
        final_component_only: Windows only: with no ``root``, accept the
            final-component guarantee (the file's folder as the root) instead of
            refusing the call. Ignored on POSIX, which only has that guarantee.

    Returns:
        The open file descriptor. The caller owns it and MUST close it.

    Raises:
        OSError: ``ELOOP`` when a link is refused (on Windows the
            :class:`~persona.errors.WorkspaceLinkRefusedError` subclass, which also
            covers a hard-linked file); plus the usual ``FileNotFoundError`` /
            ``IsADirectoryError`` / ``PermissionError`` / other ``OSError`` cases the
            caller maps to its domain response.
    """
    if sys.platform == "win32":
        return _winopen.open_fd(
            _windows_root(path, root, final_component_only), Path(path).absolute(), flags
        )
    return os.open(path, flags | os.O_NOFOLLOW, mode)


def is_regular_file_nofollow(
    path: Path, *, root: Path | None = None, final_component_only: bool = False
) -> bool:
    """Whether ``path`` is a regular file, WITHOUT following a final symlink.

    The serve/delete sites check ``Path.is_file()`` to decide a target exists —
    but ``is_file()`` *follows* a trailing symlink, so a link swapped into the
    final component after :func:`resolve_sandbox_path` would report ``True`` for an
    out-of-sandbox target (a confused-deputy). This uses ``os.lstat`` so a
    symlink (or any non-regular entry) reads as ``False``; a missing path is
    ``False`` (mirrors ``Path.is_file()``'s missing-ok semantics).

    On Windows the probe opens the file for attributes through the same no-follow
    walk as :func:`open_nofollow` (see its ``root`` semantics), so a link in any
    component below ``root`` or a hard-linked file also reads as ``False``.
    """
    if sys.platform == "win32":
        return _winopen.is_regular_file(
            _windows_root(path, root, final_component_only), Path(path).absolute()
        )
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def read_nofollow_bytes(
    path: Path, *, root: Path | None = None, final_component_only: bool = False
) -> bytes:
    """Read all bytes from ``path`` via :func:`open_nofollow` (symlink-swap safe).

    The serve/read counterpart used by the image/document/artifact download sites.
    Raises ``OSError`` (``ELOOP``) if the final component is a symlink (on Windows:
    any link below ``root``, or a hard-linked file).
    """
    fd = open_nofollow(path, os.O_RDONLY, root=root, final_component_only=final_component_only)
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


def write_nofollow_bytes(
    path: Path,
    data: bytes,
    *,
    root: Path | None = None,
    final_component_only: bool = False,
) -> None:
    """Write ``data`` to ``path`` via :func:`open_nofollow` (symlink-swap safe).

    The mirror/stage counterpart. ``O_CREAT | O_TRUNC`` overwrites an existing
    regular file; a swapped-in symlink as the final component is rejected with
    ``OSError`` (``ELOOP``) rather than clobbering its out-of-sandbox target (on
    Windows: any link below ``root``, or a hard-linked file).
    """
    fd = open_nofollow(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        root=root,
        final_component_only=final_component_only,
    )
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def read_file_under_root(relative: str, *, root: Path) -> bytes:
    """Read the workspace file at ``relative`` under ``root``, never through a link.

    For paths that come from stored records rather than the caller's own resolution
    (Spec WIN T1.5, R9-251: a replayed image's ``workspace_path``): the path is resolved
    with :func:`resolve_sandbox_path` (no escape, no Windows path shapes, no link out)
    and read with :func:`read_nofollow_bytes` under ``root``.

    Raises:
        SandboxViolationError: ``relative`` is not a safe path below ``root``.
        OSError: a link refused, a missing file, or any other filesystem error.
    """
    return read_nofollow_bytes(resolve_sandbox_path(root, relative), root=root)


def write_file_under_root(path: Path, data: bytes, *, root: Path) -> None:
    """Write ``data`` to ``path`` inside ``root``, creating missing folders, never via a link.

    For destinations the caller resolved with :func:`resolve_sandbox_path` under
    ``root`` (Spec WIN, T1.5: the produced-file copy). A ``path`` outside ``root`` is
    refused outright; the folders and the file then go through
    :func:`make_dirs_nofollow` and :func:`write_nofollow_bytes` with that root, so a
    link in the destination is refused on every platform (on POSIX a symlink as the
    final component; on Windows any link below the root) instead of being followed.

    Raises:
        ValueError: ``path`` is not inside ``root``.
        OSError: a link refused, or any other filesystem error.
    """
    root_resolved = root.resolve(strict=False)
    # ``is_relative_to`` is lexical and does not collapse ``..``: ``root/../x`` would pass
    # it. A destination from resolve_sandbox_path never contains ``..``, so any is refused.
    if ".." in Path(path).parts or not Path(path).is_relative_to(root_resolved):
        msg = "the destination is not inside the workspace root"
        raise ValueError(msg)
    make_dirs_nofollow(Path(path).parent, root=root)
    write_nofollow_bytes(Path(path), data, root=root)


def make_dirs_nofollow(
    path: Path, *, root: Path | None = None, final_component_only: bool = False
) -> None:
    """Create the folder ``path`` and any missing parents (idempotent).

    **POSIX** (unchanged): ``path.mkdir(parents=True, exist_ok=True)``, exactly what
    the callers did before; ``root`` is ignored. **Windows** (Spec WIN, T1): each
    component below ``root`` is created or opened with every reparse point refused, so
    a junction anywhere in the path stops the walk instead of creating folders outside
    the workspace. Without ``root`` the call is refused on Windows unless
    ``final_component_only=True`` (the folder's own parent becomes the root, which
    gives only the final-component guarantee).

    Raises:
        OSError: a link refused (on Windows the
            :class:`~persona.errors.WorkspaceLinkRefusedError` subclass), a file in the
            way, or any other filesystem error.
    """
    if sys.platform == "win32":
        _winopen.make_dirs(_windows_root(path, root, final_component_only), Path(path).absolute())
        return
    path.mkdir(parents=True, exist_ok=True)


def delete_regular_file_nofollow(
    path: Path, *, root: Path | None = None, final_component_only: bool = False
) -> bool:
    """Delete ``path`` if it is a regular file reached without a link; report whether it was.

    **POSIX** (unchanged): the serve/delete sites' existing sequence, the ``lstat``
    probe of :func:`is_regular_file_nofollow` then ``unlink``. **Windows** (Spec WIN,
    T1): the file is opened with every reparse point below ``root`` refused and deleted
    through that same handle, so a junction swapped into the path cannot redirect the
    delete outside the workspace; a hard-linked file is left alone (``False``).
    """
    if sys.platform == "win32":
        return _winopen.delete_regular_file(
            _windows_root(path, root, final_component_only), Path(path).absolute()
        )
    if not is_regular_file_nofollow(path, root=root, final_component_only=final_component_only):
        return False
    path.unlink()
    return True


def iter_regular_files_nofollow(base: Path) -> Iterator[tuple[Path, int]]:
    """Every regular file below the folder ``base``, with its size, for a listing.

    **POSIX** (unchanged): the storage listing's existing walk, ``rglob("*")`` keeping
    ``is_file()`` entries and their ``stat().st_size``; a missing folder yields
    nothing. **Windows** (Spec WIN, T1): ``rglob`` there descends into junctions and
    reports a symbolic link to an outside file as a file, and ``is_file``/``stat``
    follow links (to a network share, Windows would contact the host). The walk
    instead reads each entry's own attributes (``DirEntry.stat(follow_symlinks=False)``,
    which comes from the directory listing itself) and skips every reparse point, so
    it never enters or reports a link. A ``base`` that is itself a link yields nothing.
    """
    if sys.platform == "win32":
        yield from _iter_regular_files_windows(base)
        return
    if not base.is_dir():
        return
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        yield path, path.stat().st_size


def _iter_regular_files_windows(base: Path) -> Iterator[tuple[Path, int]]:
    try:
        info = os.lstat(base)
    except OSError:
        return
    if _is_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
        return
    pending = [base]
    while pending:
        folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            entry_info = entry.stat(follow_symlinks=False)
            if _is_reparse_point(entry_info):
                continue
            if stat.S_ISDIR(entry_info.st_mode):
                pending.append(Path(entry.path))
            elif stat.S_ISREG(entry_info.st_mode):
                yield Path(entry.path), entry_info.st_size


def _is_reparse_point(info: os.stat_result) -> bool:
    """Whether a no-follow ``stat`` result is a link or other reparse point (Windows only)."""
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


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
    "reserved_character": (
        'Remove characters Windows does not allow in names (: < > " | ? * and control '
        "characters); use a plain relative path like 'out/report.md'."
    ),
    "reserved_name": (
        "Windows reserves names like CON, PRN, AUX, NUL, COM1 and LPT1, even with an "
        "extension; choose a different name, e.g. 'out/report.md'."
    ),
    "trailing_dot_or_space": (
        "Remove the dot or space at the end of the file or folder name; Windows drops it "
        "and would open a different file. Use a name like 'out/report.md'."
    ),
    "link": (
        "The workspace does not follow links or shortcuts; use the file's real "
        "location inside the working directory, e.g. 'out/report.md'."
    ),
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

    if sys.platform == "win32":
        # Windows lexical rules (Spec WIN, T1.3), all before any filesystem call: on
        # Windows even resolving a UNC or device path has side effects.
        windows_violation = windows_path_violation(requested)
        if windows_violation is not None:
            summary, reason = windows_violation
            raise SandboxViolationError(
                _violation_message(summary, reason),
                context={"reason": reason, "requested": _preview(requested)},
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
    if sys.platform == "win32":
        # resolve() would let Windows FOLLOW a planted link (to a network share it would
        # authenticate to that host). Interpret links without following them instead; a
        # link to a network share or a device is refused here as an escape. The
        # no-follow opener still refuses every link below the root at open (Spec WIN, T1).
        walk = resolve_without_following(root_resolved, requested)
        if walk.path is None:
            raise SandboxViolationError(
                _violation_message("path escapes sandbox", "escape"),
                context={
                    "reason": "escape",
                    "requested": _preview(requested),
                    "resolved": "<a link to a network share or device>",
                },
            )
        if walk.crossed_link and walk.path.is_relative_to(root_resolved):
            # Ruling 3: a link that stays inside is refused too. (One leading outside
            # falls through to the escape check below, with its usual reason.)
            raise SandboxViolationError(
                _violation_message("path goes through a link or shortcut", "link"),
                context={"reason": "link", "requested": _preview(requested)},
            )
        candidate = walk.path
    else:
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
