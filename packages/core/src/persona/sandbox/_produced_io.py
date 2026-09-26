"""Reading the local sandbox's output folder without following links (Spec WIN T1.6, X-1).

The local Docker sandbox bind-mounts a host folder as the container's ``/workspace/out``.
Code in the container controls everything in that folder, including links: a symlink
``out/leak.txt -> /srv/persona/.env`` must never make the HOST read its own secret and
copy it into the persona workspace. A background process in the container can also
swap a folder for a link between the listing and the read.

- :func:`list_produced_files` walks with ``os.scandir`` and each entry's OWN attributes
  (``stat(follow_symlinks=False)``): it never enters or reports a symbolic link, a
  junction or any other reparse point, and keeps regular files only.
- :func:`read_produced_file` opens without following a link in ANY component: on POSIX
  it walks folder by folder with ``dir_fd`` and ``O_NOFOLLOW`` (``O_NONBLOCK`` so a FIFO
  planted in the folder cannot hang the host), on Windows it uses the handle-relative
  opener. The opened file must be a regular file with exactly one name, and the size cap
  is checked on the open descriptor, so nothing can change between check and read.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path

from persona.sandbox.errors import (
    CodeSandboxError,
    ProducedFileRefusedError,
    ProducedFileSizeError,
)
from persona.sandbox.result import produced_file_path_violation

__all__ = ["list_produced_files", "read_produced_file"]

_READ_CHUNK = 1 << 20
#: Errors that mean "a link, or something that is not a folder, sits in the path".
_LINK_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.ENXIO})


def _is_link_or_reparse(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def list_produced_files(host_out: Path) -> list[tuple[str, int]]:
    """Every regular file below ``host_out``: ``(relative posix path, size)``, sorted.

    Sorted like the ``Path`` objects the previous ``rglob`` walk returned (by path
    parts). A missing ``host_out``, or one that is itself a link, yields nothing.
    """
    try:
        base_info = os.lstat(host_out)
    except OSError:
        return []
    if _is_link_or_reparse(base_info) or not stat.S_ISDIR(base_info.st_mode):
        return []
    found: list[tuple[str, int]] = []
    pending: list[tuple[Path, tuple[str, ...]]] = [(host_out, ())]
    while pending:
        folder, prefix = pending.pop()
        try:
            with os.scandir(folder) as entries:
                listed = list(entries)
        except OSError:
            continue
        for entry in listed:
            info = entry.stat(follow_symlinks=False)
            if _is_link_or_reparse(info):
                continue
            parts = (*prefix, entry.name)
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(entry.path), parts))
            elif stat.S_ISREG(info.st_mode):
                found.append(("/".join(parts), info.st_size))
    return sorted(found, key=lambda item: item[0].split("/"))


def _refused(ref: str, session_id: str) -> ProducedFileRefusedError:
    return ProducedFileRefusedError(
        "produced file refused",
        context={
            "ref": ref,
            "session_id": session_id,
            "reason": "it is a link, a shortcut or not a plain file",
        },
    )


def _missing(ref: str, session_id: str) -> CodeSandboxError:
    return CodeSandboxError(
        f"produced file {ref!r} not found in session {session_id!r}",
        context={"reason": "produced_file_missing", "session_id": session_id, "ref": ref},
    )


def _open_no_follow(host_out: Path, parts: list[str]) -> int:
    """Open ``host_out/<parts>`` read-only, refusing a link in any component."""
    if sys.platform == "win32":
        from persona.tools._sandbox import open_nofollow  # noqa: PLC0415

        return open_nofollow(host_out.joinpath(*parts), os.O_RDONLY, root=host_out)
    folder = os.open(host_out, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=folder)
            os.close(folder)
            folder = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    finally:
        os.close(folder)


def read_produced_file(host_out: Path, ref: str, *, session_id: str, cap_bytes: int) -> bytes:
    """The bytes of the produced file ``ref`` below ``host_out``, never through a link.

    Raises:
        ProducedFileRefusedError: ``ref`` is not a safe name, a link sits anywhere in its
            path, or it is not a regular file with exactly one name (the tool then skips
            this one file and tells the model why).
        ProducedFileSizeError: the file is larger than ``cap_bytes``.
        CodeSandboxError: the file does not exist.
    """
    if produced_file_path_violation(ref) is not None:
        raise _refused(ref, session_id)
    try:
        fd = _open_no_follow(host_out, [p for p in ref.split("/") if p not in ("", ".")])
    except FileNotFoundError as exc:
        raise _missing(ref, session_id) from exc
    except OSError as exc:
        if exc.errno in _LINK_ERRNOS:
            raise _refused(ref, session_id) from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _refused(ref, session_id)
        if info.st_size > cap_bytes:
            raise ProducedFileSizeError(
                f"produced file {ref!r} is {info.st_size} bytes, exceeds {cap_bytes}-byte cap",
                context={
                    "ref": ref,
                    "size_bytes": str(info.st_size),
                    "cap_bytes": str(cap_bytes),
                    "session_id": session_id,
                },
            )
        chunks: list[bytes] = []
        while chunk := os.read(fd, _READ_CHUNK):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)
