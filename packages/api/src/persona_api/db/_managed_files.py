"""The bundled database's folders and private files, checked before use (Spec WIN, T2 review).

On Windows (the only platform that runs :mod:`persona_api.db._managed_windows` in
production) this is where Postgres's compiled-out ``checkDataDir`` is supplied:

- the base folder and the data folder are created with the owner-and-SYSTEM DACL
  applied atomically; an existing one is refused unless the rules in
  :mod:`persona_api.db._datadir_guard` accept its owner and access list;
- the data folder then gets the owner-only DACL, and the result is verified;
- the password file and the staged initdb password are created with an exclusive
  create (never over an existing file), and an existing one is read only after its
  owner and access list are checked through the very handle it is read from.

Elsewhere the same functions keep the POSIX meaning (0700 folders, a file owned by
this uid with no group or other bits), so the lifecycle's unit tests run on Linux.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING

from persona.tools._sandbox import delete_regular_file_nofollow, open_nofollow

from persona_api.db._datadir_guard import (
    base_folder_refusal,
    datadir_refusal,
    private_file_refusal,
    restricted_datadir_refusal,
)
from persona_api.errors import CommunityDbError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = ["create_private_file", "prepared_folders", "read_private_file", "remove_own_file"]

_READ_CHUNK = 1 << 16


def _refuse(message: str, reason: str, path: Path) -> CommunityDbError:
    return CommunityDbError(message, context={"reason": reason, "path": str(path)})


@contextmanager
def prepared_folders(datadir: Path) -> Iterator[None]:
    """Create or check the base folder and the data folder, and hold them while in use.

    On Windows every missing folder down to the base is created owner-only and
    atomically. A handle on the base is opened BEFORE the base is checked, and a handle
    on the data folder before the data folder is checked; both stay open until the
    ``with`` block ends. While they are open Windows refuses to rename any folder on
    their path, so a folder that passed the checks cannot be swapped for another
    before the server is up (review N-1). The data folder is only created once the base
    has passed. The caller keeps the block open until the postmaster runs.

    Raises:
        CommunityDbError: a folder belongs to another account, lets another account
            change what is in it, or could not be limited to this account.
    """
    if sys.platform != "win32":
        datadir.parent.parent.mkdir(parents=True, exist_ok=True)
        datadir.parent.mkdir(mode=0o700, exist_ok=True)
        datadir.mkdir(mode=0o700, exist_ok=True)
        yield
        return
    from persona.tools import _winacl

    base = datadir.parent
    user = _winacl.current_user_sid()
    _create_missing_owner_only(base)
    base_handle = _winacl.hold_folder(base)
    try:
        refusal = base_folder_refusal(_winacl.folder_security(base), user, base)
        if refusal:
            raise _refuse(refusal, "unsafe_base_folder", base)
        _winacl.create_owner_only_folder(datadir)
        data_handle = _winacl.hold_folder(datadir)
        try:
            _check_data_folder(datadir, user)
            yield
        finally:
            _winacl.release_folder(data_handle)
    finally:
        _winacl.release_folder(base_handle)


def _create_missing_owner_only(folder: Path) -> None:
    """Create each missing folder from the top down to ``folder``, owner-only and atomic."""
    from persona.tools import _winacl

    missing = [each for each in (folder, *folder.parents) if not each.exists()]
    for each in reversed(missing):
        _winacl.create_owner_only_folder(each)


def _check_data_folder(datadir: Path, user: str) -> None:
    from persona.tools import _winacl

    refusal = datadir_refusal(_winacl.folder_security(datadir), user, datadir)
    if refusal:
        raise _refuse(refusal, "unsafe_data_folder", datadir)
    _winacl.restrict_folder_to_owner(datadir)
    refusal = restricted_datadir_refusal(_winacl.folder_security(datadir), user, datadir)
    if refusal:
        raise _refuse(refusal, "unsafe_data_folder", datadir)


def _check_own_file(fd: int, path: Path) -> None:
    """Refuse the open file unless it is ours alone (checked through ``fd``, no re-open)."""
    if sys.platform == "win32":
        from persona.tools import _winacl

        summary = _winacl.fd_security(fd, str(path))
        refusal = private_file_refusal(summary, _winacl.current_user_sid(), path)
    else:
        status = os.fstat(fd)
        refusal = None
        if status.st_uid != os.getuid() or status.st_mode & 0o077:
            refusal = (
                f"The password file {path} is not private to your account, so the bundled "
                "database will not trust it."
            )
    if refusal:
        raise _refuse(refusal, "unsafe_private_file", path)


def read_private_file(path: Path) -> bytes | None:
    """The bytes of ``path`` if it exists and is ours alone; ``None`` if it does not exist.

    Raises:
        CommunityDbError: the file exists but another account made it or can open it.
        OSError: it is a link or could not be read.
    """
    try:
        fd = open_nofollow(path, os.O_RDONLY, root=path.parent)
    except FileNotFoundError:
        return None
    try:
        _check_own_file(fd, path)
        chunks: list[bytes] = []
        while chunk := os.read(fd, _READ_CHUNK):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def create_private_file(path: Path, data: bytes) -> None:
    """Create ``path`` owner-only with ``data``; never open or replace an existing file.

    Raises:
        CommunityDbError: something already has that name.
    """
    try:
        fd = open_nofollow(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, root=path.parent)
    except FileExistsError as exc:
        raise _refuse(
            f"{path} appeared while the bundled database was creating it. Something else is "
            "writing in that folder; check it, then start again.",
            "private_file_exists",
            path,
        ) from exc
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


def remove_own_file(path: Path) -> None:
    """Delete ``path`` if it exists, after checking it is ours alone.

    Raises:
        CommunityDbError: it exists but another account made it or can open it.
    """
    if read_private_file(path) is None:
        return
    delete_regular_file_nofollow(path, root=path.parent)
