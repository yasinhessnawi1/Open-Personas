"""The Windows no-follow file primitives behind the sandbox opener (Spec WIN, T1).

POSIX closes the symlink-swap window with ``O_NOFOLLOW`` (R2 F-03). Windows has no
such flag, and its version of the threat is wider: symbolic links, directory
junctions and other reparse points can sit in ANY path component, a link can point
at a network share (following it makes Windows authenticate to that host), and a
hard link can give an outside file a name inside the workspace.

Every operation here therefore works relative to a handle on the workspace root:

1. The root is opened once with ``CreateFileW`` (it is operator configuration, so it
   is trusted, and following links above it is intended).
2. The path below the root is opened with ``NtCreateFile`` relative to that handle,
   with ``OBJ_DONT_REPARSE``: the kernel refuses the open if it meets a reparse
   point in any component, final or intermediate, before following it. This is the
   Windows counterpart of Linux ``openat2(RESOLVE_NO_SYMLINKS)`` and it leaves no
   window between a check and the use: there is no separate check.
3. The opened file is verified by handle: not a reparse point, not a directory, and
   exactly one name (a hard-linked file is refused).
4. The handle becomes a BINARY C runtime descriptor (``msvcrt.open_osfhandle``
   without ``O_TEXT``), so bytes are never translated, and it is not inheritable.

A file this module creates gets an owner-only DACL (the current user and SYSTEM),
applied atomically at creation: the Windows reading of ``0o600``.

Only documented APIs are used: ``NtCreateFile`` and ``OBJECT_ATTRIBUTES``
(``OBJ_DONT_REPARSE``), ``RtlNtStatusToDosError``, ``CreateFileW``,
``GetFileInformationByHandleEx``, ``SetFileInformationByHandle``, ``CloseHandle``,
``OpenProcessToken``, ``GetTokenInformation``, ``ConvertSidToStringSidW``,
``ConvertStringSecurityDescriptorToSecurityDescriptorW`` and ``LocalFree``.

This module is imported only on Windows (by :mod:`persona.tools._sandbox`).
"""

from __future__ import annotations

import sys

if sys.platform != "win32":  # pragma: no cover - the module is Windows-only by design
    raise ImportError("persona.tools._winopen is only available on Windows")

import ctypes
import errno
import msvcrt
import os
from ctypes import wintypes
from pathlib import Path, PureWindowsPath
from typing import NamedTuple

from persona.errors import WorkspaceLinkRefusedError
from persona.tools._winpath import windows_component_violation

__all__ = [
    "LinkFound",
    "delete_regular_file",
    "find_first_link",
    "is_regular_file",
    "make_dirs",
    "open_fd",
    "win32_link_target",
]

# --- access rights, share modes, dispositions and options (winnt.h / ntifs.h) -----------
_SYNCHRONIZE = 0x00100000
_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_LIST_DIRECTORY = 0x0001
_FILE_TRAVERSE = 0x0020
_FILE_GENERIC_READ = 0x00120089
_FILE_GENERIC_WRITE = 0x00120116
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_FILE_SHARE_DELETE = 0x4
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPEN_IF = 3
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_REPARSE_POINT = 0x00200000
_OBJ_CASE_INSENSITIVE = 0x00000040
_OBJ_DONT_REPARSE = 0x00001000
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_TOKEN_QUERY = 0x0008
_TOKEN_USER_CLASS = 1
_SDDL_REVISION_1 = 1
_FILE_STANDARD_INFO_CLASS = 1
_FILE_DISPOSITION_INFO_CLASS = 4
_FILE_DISPOSITION_INFO_EX_CLASS = 21
_FILE_DISPOSITION_FLAG_DELETE = 0x00000001
_FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE = 0x00000010
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_MAX_REPARSE_BUFFER = 16 * 1024
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
#: ``UNICODE_STRING`` lengths are 16-bit byte counts (the largest even one).
_MAX_UNICODE_STRING_BYTES = 0xFFFE
#: Errors that mean "this ``FILE_DISPOSITION_INFO_EX`` is not supported here".
_DISPOSITION_EX_UNSUPPORTED = frozenset({1, 50, 87})
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9

# NTSTATUS values meaning "a reparse point was met and not followed".
_STATUS_REPARSE_POINT_ENCOUNTERED = 0xC000050B
_STATUS_IO_REPARSE_TAG_NOT_HANDLED = 0xC0000279
_STATUS_STOPPED_ON_SYMLINK = 0x8000002D
_LINK_STATUSES = frozenset(
    {
        _STATUS_REPARSE_POINT_ENCOUNTERED,
        _STATUS_IO_REPARSE_TAG_NOT_HANDLED,
        _STATUS_STOPPED_ON_SYMLINK,
    }
)
_STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
_STATUS_NOT_A_DIRECTORY = 0xC0000103

# os.open flags this opener understands. Anything else (O_APPEND, O_TEXT, O_TEMPORARY,
# O_SHORT_LIVED, O_RANDOM, O_SEQUENTIAL, ...) is refused loudly rather than ignored, so
# no flag can silently change meaning on Windows (text mode above all).
_ACCESS_MASK = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
_SUPPORTED_FLAGS = _ACCESS_MASK | os.O_CREAT | os.O_EXCL | os.O_TRUNC | os.O_BINARY | os.O_NOINHERIT

_LINK_MESSAGE = (
    "This path goes through a link or shortcut, and the workspace does not follow "
    "links or shortcuts. Use a plain file inside the working directory."
)
_HARD_LINK_MESSAGE = (
    "This file also exists under another name elsewhere (a hard link), and the "
    "workspace does not open such files. Use a plain file inside the working directory."
)
_NOT_REGULAR_MESSAGE = "This path is not a plain file, so the workspace will not open it."


# --- ctypes declarations ---------------------------------------------------------------
class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class _FileStandardInfo(ctypes.Structure):
    _fields_ = [
        ("AllocationSize", ctypes.c_longlong),
        ("EndOfFile", ctypes.c_longlong),
        ("NumberOfLinks", wintypes.DWORD),
        ("DeletePending", wintypes.BOOLEAN),
        ("Directory", wintypes.BOOLEAN),
    ]


class _FileDispositionInfo(ctypes.Structure):
    _fields_ = [("DeleteFile", wintypes.BOOLEAN)]


class _FileDispositionInfoEx(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD)]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


_ntdll = ctypes.WinDLL("ntdll")
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_NtCreateFile = _ntdll.NtCreateFile
_NtCreateFile.restype = ctypes.c_long
_NtCreateFile.argtypes = [
    ctypes.POINTER(wintypes.HANDLE),
    wintypes.DWORD,
    ctypes.POINTER(_ObjectAttributes),
    ctypes.POINTER(_IoStatusBlock),
    ctypes.c_void_p,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    ctypes.c_void_p,
    wintypes.ULONG,
]
_RtlNtStatusToDosError = _ntdll.RtlNtStatusToDosError
_RtlNtStatusToDosError.restype = wintypes.ULONG
_RtlNtStatusToDosError.argtypes = [ctypes.c_long]

_CreateFileW = _kernel32.CreateFileW
_CreateFileW.restype = wintypes.HANDLE
_CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_CloseHandle = _kernel32.CloseHandle
_CloseHandle.restype = wintypes.BOOL
_CloseHandle.argtypes = [wintypes.HANDLE]
_GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
_GetFileInformationByHandleEx.restype = wintypes.BOOL
_GetFileInformationByHandleEx.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_SetFileInformationByHandle = _kernel32.SetFileInformationByHandle
_SetFileInformationByHandle.restype = wintypes.BOOL
_SetFileInformationByHandle.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
]
_DeviceIoControl = _kernel32.DeviceIoControl
_DeviceIoControl.restype = wintypes.BOOL
_DeviceIoControl.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.c_void_p,
]
_GetCurrentProcess = _kernel32.GetCurrentProcess
_GetCurrentProcess.restype = wintypes.HANDLE
_GetCurrentProcess.argtypes = []
_LocalFree = _kernel32.LocalFree
_LocalFree.restype = ctypes.c_void_p
_LocalFree.argtypes = [ctypes.c_void_p]

_OpenProcessToken = _advapi32.OpenProcessToken
_OpenProcessToken.restype = wintypes.BOOL
_OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
_GetTokenInformation = _advapi32.GetTokenInformation
_GetTokenInformation.restype = wintypes.BOOL
_GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_ConvertSidToStringSidW = _advapi32.ConvertSidToStringSidW
_ConvertSidToStringSidW.restype = wintypes.BOOL
_ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
_ConvertSddlToSd = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
_ConvertSddlToSd.restype = wintypes.BOOL
_ConvertSddlToSd.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.ULONG),
]

_INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


# --- small helpers ---------------------------------------------------------------------
def _last_error(filename: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(0, ctypes.FormatError(code).strip(), filename, code)


def _close(handle: int) -> None:
    _CloseHandle(handle)


def _preview(path: Path) -> str:
    text = "".join(c for c in str(path) if c == "\t" or ord(c) >= 32)
    return text if len(text) <= 160 else text[:160] + "...<truncated>"


def _link_refused(
    path: Path, reason: str, message: str = _LINK_MESSAGE
) -> WorkspaceLinkRefusedError:
    return WorkspaceLinkRefusedError(
        message, context={"reason": reason, "path": _preview(path)}, filename=str(path)
    )


def _status_error(status: int, path: Path) -> OSError:
    """Map a failing NTSTATUS to the error a POSIX caller would see for the same case."""
    if status in _LINK_STATUSES:
        return _link_refused(path, "link_refused")
    if status == _STATUS_FILE_IS_A_DIRECTORY:
        return IsADirectoryError(errno.EISDIR, "Is a directory", str(path))
    if status == _STATUS_NOT_A_DIRECTORY:
        return NotADirectoryError(errno.ENOTDIR, "Not a directory", str(path))
    code = int(_RtlNtStatusToDosError(ctypes.c_long(status).value))
    return OSError(0, ctypes.FormatError(code).strip(), str(path), code)


def _relative_name(root: Path, path: Path) -> str:
    """The backslash-joined name of ``path`` below ``root`` (both absolute, same volume).

    The resolver has already proven ``path`` is inside ``root``; this re-checks the
    shape as defence in depth, because the name is handed to the kernel as-is.
    """
    parts = PureWindowsPath(path).relative_to(PureWindowsPath(root)).parts
    if not parts:
        raise ValueError(f"no file below the workspace root: {_preview(path)}")
    for part in parts:
        if part in (".", "..") or any(c in part for c in "\\/:") or any(ord(c) < 32 for c in part):
            raise ValueError(f"unsafe path component below the workspace root: {part!r}")
        if windows_component_violation(part) is not None:
            # Trailing dot or space, or a reserved device name: NT would create exactly
            # that name, which Win32 later aliases to another file or to a device.
            raise ValueError(f"unsafe path component below the workspace root: {part!r}")
    return "\\".join(parts)


def _open_root(root: Path) -> int:
    """Open the (trusted) workspace root directory; the caller must close the handle."""
    handle = _CreateFileW(
        str(root),
        _FILE_TRAVERSE | _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        raise _last_error(str(root))
    return int(handle)


def _current_user_sid() -> str:
    """The string SID of the user this process runs as (``TokenUser``, not the owner group).

    ``TokenUser`` is used rather than the ``OW`` (owner rights) alias because an elevated
    process's default owner is the Administrators group: a file owned that way would lock
    the same user out of it when the app later runs unelevated.
    """
    token = wintypes.HANDLE()
    if not _OpenProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise _last_error("process token")
    try:
        needed = wintypes.DWORD(0)
        _GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not _GetTokenInformation(token, _TOKEN_USER_CLASS, buffer, needed, ctypes.byref(needed)):
            raise _last_error("process token")
        user = _SidAndAttributes.from_buffer(buffer)
        text = wintypes.LPWSTR()
        if not _ConvertSidToStringSidW(user.Sid, ctypes.byref(text)):
            raise _last_error("process token")
        try:
            return str(text.value)
        finally:
            _LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        _close(int(token.value or 0))


def _owner_only_descriptor() -> int:
    """A security descriptor granting only this user and SYSTEM; the caller LocalFrees it.

    ``D:P`` makes the DACL protected, so nothing is inherited from the folder: the
    Windows reading of ``0o600`` (owner read/write, the system account still able to act).
    """
    sddl = f"D:P(A;;FA;;;{_current_user_sid()})(A;;FA;;;SY)"
    descriptor = ctypes.c_void_p()
    if not _ConvertSddlToSd(sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None):
        raise _last_error("security descriptor")
    return int(descriptor.value or 0)


def _nt_open(
    root_handle: int,
    name: str,
    path: Path,
    *,
    access: int,
    share: int,
    disposition: int,
    options: int,
    descriptor: int | None = None,
) -> int:
    """``NtCreateFile`` of ``name`` relative to ``root_handle``, refusing every reparse point."""
    size = len(name) * ctypes.sizeof(ctypes.c_wchar)
    if size > _MAX_UNICODE_STRING_BYTES:
        # A UNICODE_STRING length is 16 bits; a longer name would wrap silently.
        raise OSError(errno.ENAMETOOLONG, "path too long for the workspace", str(path))
    buffer = ctypes.create_unicode_buffer(name)
    # ``buffer`` stays referenced until the call returns, so the cast pointer stays valid.
    object_name = _UnicodeString(
        size, size + ctypes.sizeof(ctypes.c_wchar), ctypes.cast(buffer, wintypes.LPWSTR)
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        root_handle,
        ctypes.pointer(object_name),
        _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
        descriptor,
        None,
    )
    status_block = _IoStatusBlock()
    handle = wintypes.HANDLE()
    status = _NtCreateFile(
        ctypes.byref(handle),
        access | _SYNCHRONIZE,
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        _FILE_ATTRIBUTE_NORMAL,
        share,
        disposition,
        options | _FILE_SYNCHRONOUS_IO_NONALERT,
        None,
        0,
    )
    if status < 0:
        raise _status_error(status & 0xFFFFFFFF, path)
    return int(handle.value or 0)


def _check_regular_single_name(handle: int, path: Path) -> None:
    """Refuse a reparse point, a directory or a hard-linked file, by handle (no path reuse)."""
    tag = _FileAttributeTagInfo()
    if not _GetFileInformationByHandleEx(
        handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(tag), ctypes.sizeof(tag)
    ):
        raise _last_error(str(path))
    if tag.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise _link_refused(path, "link_refused")
    if tag.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise _link_refused(path, "not_regular", _NOT_REGULAR_MESSAGE)
    standard = _FileStandardInfo()
    if not _GetFileInformationByHandleEx(
        handle, _FILE_STANDARD_INFO_CLASS, ctypes.byref(standard), ctypes.sizeof(standard)
    ):
        raise _last_error(str(path))
    if standard.NumberOfLinks != 1:
        raise _link_refused(path, "hard_link_refused", _HARD_LINK_MESSAGE)


def _access_and_disposition(flags: int) -> tuple[int, int]:
    unsupported = flags & ~_SUPPORTED_FLAGS
    if unsupported:
        raise ValueError(f"os.open flags not supported by the workspace opener: {unsupported:#x}")
    mode = flags & _ACCESS_MASK
    if mode == os.O_RDONLY:
        access = _FILE_GENERIC_READ
    elif mode == os.O_WRONLY:
        access = _FILE_GENERIC_WRITE | _FILE_READ_ATTRIBUTES
    else:
        access = _FILE_GENERIC_READ | _FILE_GENERIC_WRITE
    if flags & os.O_CREAT:
        disposition = _FILE_CREATE if flags & os.O_EXCL else _FILE_OPEN_IF
    else:
        disposition = _FILE_OPEN
    return access, disposition


def _to_fd(handle: int, path: Path, *, read_only: bool, truncate: bool) -> int:
    """Verify the opened file, then hand the handle to the C runtime as a binary fd."""
    try:
        _check_regular_single_name(handle, path)
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY if read_only else 0)
    except BaseException:
        _close(handle)
        raise
    try:
        if truncate:
            os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


# --- the operations --------------------------------------------------------------------
def open_fd(root: Path, path: Path, flags: int) -> int:
    """Open ``path`` (inside ``root``) with ``os.open``-style ``flags``; return a binary fd.

    Refuses (with :class:`~persona.errors.WorkspaceLinkRefusedError`) a reparse point in
    any component below ``root`` and a hard-linked file. ``O_TRUNC`` is applied only
    after those checks pass, so a refused file is never modified. A created file gets
    the owner-only DACL. The caller owns the returned descriptor and must close it.
    """
    access, disposition = _access_and_disposition(flags)
    name = _relative_name(root, path)
    descriptor = _owner_only_descriptor() if flags & os.O_CREAT else None
    try:
        root_handle = _open_root(root)
        try:
            handle = _nt_open(
                root_handle,
                name,
                path,
                access=access,
                share=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
                disposition=disposition,
                options=_FILE_NON_DIRECTORY_FILE,
                descriptor=descriptor,
            )
        finally:
            _close(root_handle)
    finally:
        if descriptor:
            _LocalFree(descriptor)
    return _to_fd(
        handle, path, read_only=access == _FILE_GENERIC_READ, truncate=bool(flags & os.O_TRUNC)
    )


def make_dirs(root: Path, path: Path) -> None:
    """Create ``path`` and its missing parents below ``root``, one component at a time.

    Each component is created or opened relative to the root handle with every
    reparse point refused, so a junction swapped in anywhere stops the walk instead of
    creating folders outside the workspace. The root itself is created if missing (it
    is trusted configuration). ``path == root`` is a no-op.
    """
    root.mkdir(parents=True, exist_ok=True)
    if PureWindowsPath(path) == PureWindowsPath(root):
        return
    parts = _relative_name(root, path).split("\\")
    root_handle = _open_root(root)
    try:
        for depth in range(1, len(parts) + 1):
            handle = _nt_open(
                root_handle,
                "\\".join(parts[:depth]),
                path,
                access=_FILE_LIST_DIRECTORY | _FILE_TRAVERSE,
                share=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                disposition=_FILE_OPEN_IF,
                options=_FILE_DIRECTORY_FILE,
            )
            _close(handle)
    finally:
        _close(root_handle)


def _open_existing_for(root: Path, path: Path, access: int) -> int | None:
    """Open an existing plain single-name file below ``root``; ``None`` when it is not one."""
    try:
        name = _relative_name(root, path)
        root_handle = _open_root(root)
    except (OSError, ValueError):
        return None
    try:
        handle = _nt_open(
            root_handle,
            name,
            path,
            access=access,
            share=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            disposition=_FILE_OPEN,
            options=_FILE_NON_DIRECTORY_FILE,
        )
    except OSError:
        return None
    finally:
        _close(root_handle)
    try:
        _check_regular_single_name(handle, path)
    except OSError:
        _close(handle)
        return None
    return handle


def is_regular_file(root: Path, path: Path) -> bool:
    """Whether ``path`` is a plain, single-name file below ``root`` reached through no link.

    Mirrors the POSIX ``lstat`` probe's missing-is-``False`` semantics: any refusal or
    error reads as ``False``.
    """
    handle = _open_existing_for(root, path, _FILE_READ_ATTRIBUTES)
    if handle is None:
        return False
    _close(handle)
    return True


def delete_regular_file(root: Path, path: Path) -> bool:
    """Delete ``path`` if it is a plain single-name file below ``root``; report whether it was.

    The file is opened with every reparse point refused and deleted through that same
    handle, so a junction swapped into the path cannot redirect the delete outside the
    workspace.
    """
    handle = _open_existing_for(root, path, _DELETE | _FILE_READ_ATTRIBUTES)
    if handle is None:
        return False
    try:
        _mark_for_deletion(handle, path)
    finally:
        _close(handle)
    return True


def _mark_for_deletion(handle: int, path: Path) -> None:
    """Delete through the handle, ignoring the read-only attribute as POSIX unlink does.

    ``FILE_DISPOSITION_INFO_EX`` with ``IGNORE_READONLY_ATTRIBUTE`` (Windows 10 1809+)
    deletes a read-only file; where it is not supported, the classic disposition is
    used, which deletes everything except a read-only file (``PermissionError``).
    """
    extended = _FileDispositionInfoEx(
        _FILE_DISPOSITION_FLAG_DELETE | _FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE
    )
    if _SetFileInformationByHandle(
        handle, _FILE_DISPOSITION_INFO_EX_CLASS, ctypes.byref(extended), ctypes.sizeof(extended)
    ):
        return
    if ctypes.get_last_error() not in _DISPOSITION_EX_UNSUPPORTED:
        raise _last_error(str(path))
    classic = _FileDispositionInfo(True)
    if not _SetFileInformationByHandle(
        handle, _FILE_DISPOSITION_INFO_CLASS, ctypes.byref(classic), ctypes.sizeof(classic)
    ):
        raise _last_error(str(path))


class LinkFound(NamedTuple):
    """The first reparse point below the root: where it is and what it names.

    Attributes:
        position: Where the linked component sits in the walked parts.
        target: The link's raw target (a symbolic link's or junction's substitute
            name, in Win32 form), or ``None`` for any other reparse point or a target
            that could not be read.
    """

    position: int
    target: str | None


def find_first_link(root: Path, parts: tuple[str, ...]) -> LinkFound | None:
    """Find the first reparse point among the existing components of ``parts`` below ``root``.

    Every component is opened relative to its parent's HANDLE with ``OBJ_DONT_REPARSE``
    and ``FILE_OPEN_REPARSE_POINT``: a link is opened as itself, never followed, and no
    Win32 path is ever parsed, so a component swapped for a link (even one pointing at a
    network share) after its parent was opened cannot make Windows follow it or contact
    anyone. The walk stops at the first missing component (the rest does not exist) and
    at the first reparse point, whose target is read through its own handle
    (``FSCTL_GET_REPARSE_POINT``). A root that does not exist yet holds no link.
    Advisory only: the opener enforces at open time.
    """
    try:
        root_handle = _open_root(root)
    except OSError:
        return None  # no root yet (a first write creates it): nothing below it to walk
    parent = root_handle
    try:
        for index, part in enumerate(parts):
            try:
                handle = _nt_open(
                    parent,
                    part,
                    root / part,
                    access=_FILE_READ_ATTRIBUTES,
                    share=_FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                    disposition=_FILE_OPEN,
                    options=_FILE_OPEN_REPARSE_POINT,
                )
            except OSError:
                return None  # missing, or not a folder: nothing further to walk
            try:
                attributes, tag = _attribute_tag(handle, root / part)
            except OSError:
                # Its kind cannot be read: close it and treat it as an unreadable link,
                # which the resolver refuses (a sandbox violation, never a raw OSError).
                _close(handle)
                return LinkFound(position=index, target=None)
            if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                try:
                    target = (
                        _reparse_target(handle)
                        if tag in (_IO_REPARSE_TAG_SYMLINK, _IO_REPARSE_TAG_MOUNT_POINT)
                        else None
                    )
                finally:
                    _close(handle)
                return LinkFound(position=index, target=target)
            if parent != root_handle:
                _close(parent)
            parent = handle
        return None
    finally:
        if parent != root_handle:
            _close(parent)
        _close(root_handle)


def _attribute_tag(handle: int, path: Path) -> tuple[int, int]:
    info = _FileAttributeTagInfo()
    if not _GetFileInformationByHandleEx(
        handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise _last_error(str(path))
    return int(info.FileAttributes), int(info.ReparseTag)


def _reparse_target(handle: int) -> str | None:
    """A symbolic link's or junction's target, read from its reparse data by handle.

    The substitute name is used (the one the system acts on) and turned into Win32
    form: ``\\??\\C:\\x`` becomes ``C:\\x`` and ``\\??\\UNC\\host\\share``
    becomes ``\\\\host\\share``; a relative symbolic link stays relative.
    """
    buffer = ctypes.create_string_buffer(_MAX_REPARSE_BUFFER)
    returned = wintypes.DWORD()
    if not _DeviceIoControl(
        handle,
        _FSCTL_GET_REPARSE_POINT,
        None,
        0,
        buffer,
        _MAX_REPARSE_BUFFER,
        ctypes.byref(returned),
        None,
    ):
        return None
    raw = buffer.raw[: returned.value]
    if len(raw) < 16:
        return None
    tag = int.from_bytes(raw[0:4], "little")
    offset = int.from_bytes(raw[8:10], "little")
    length = int.from_bytes(raw[10:12], "little")
    path_buffer = 20 if tag == _IO_REPARSE_TAG_SYMLINK else 16
    start, end = path_buffer + offset, path_buffer + offset + length
    if end > len(raw):
        return None
    return win32_link_target(raw[start:end].decode("utf-16-le", errors="replace"))


def win32_link_target(substitute_name: str) -> str | None:
    """A link's substitute name in the Win32 form the walk classifies, or ``None``.

    ``\\??\\C:\\x`` becomes ``C:\\x`` and ``\\??\\UNC\\host\\share`` becomes
    ``\\\\host\\share`` (a network share, refused later); a relative link stays
    relative. Anything else in the NT namespace (``GLOBALROOT``, ``Volume{...}``, a
    device) is ``None``: not a path this walk classifies, so it is refused.
    """
    if substitute_name.startswith("\\??\\UNC\\"):
        return "\\\\" + substitute_name[len("\\??\\UNC\\") :]
    if substitute_name.startswith("\\??\\"):
        rest = substitute_name[len("\\??\\") :]
        if len(rest) >= 2 and rest[1] == ":" and rest[0].isalpha():
            return rest
        return None
    return substitute_name
