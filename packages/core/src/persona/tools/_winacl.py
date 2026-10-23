"""Windows folder ownership and access, read and set by handle (Spec WIN, T2 security review).

POSIX Postgres refuses a data folder it does not own or that others can open
(``checkDataDir``); that check is compiled out on Windows, so the product supplies it.
This module holds the Windows primitives for it:

- :func:`create_owner_only_folder` creates a folder whose owner is this user and whose
  protected DACL grants only this user and SYSTEM, applied atomically at creation, so
  there is no moment when the folder exists with inherited access.
- :func:`folder_security` and :func:`fd_security` read an object's owner and DACL by
  handle. The folder is opened without following a reparse point and a reparse point is
  refused, so the answer is about the folder itself, never about a link's target.
- :func:`restrict_folder_to_owner` replaces a folder's DACL with the protected
  owner-and-SYSTEM one and propagates it to what is inside.
- :func:`hold_folder` pins a folder and every folder above it against rename while
  checks made on its path are relied on.
- :func:`process_image` names the executable of a running process, so a stale
  ``postmaster.pid`` whose PID was reused is not mistaken for a running server.

Imported only on Windows.
"""

from __future__ import annotations

import sys

if sys.platform != "win32":  # pragma: no cover - the module is Windows-only by design
    raise ImportError("persona.tools._winacl is only available on Windows")

import ctypes
import errno
import msvcrt
from ctypes import wintypes
from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona.tools._winopen import (
    _FILE_ATTRIBUTE_DIRECTORY,
    _FILE_ATTRIBUTE_REPARSE_POINT,
    _FILE_ATTRIBUTE_TAG_INFO_CLASS,
    _FILE_FLAG_BACKUP_SEMANTICS,
    _FILE_OPEN_REPARSE_POINT,
    _FILE_READ_ATTRIBUTES,
    _FILE_SHARE_DELETE,
    _FILE_SHARE_READ,
    _FILE_SHARE_WRITE,
    _INVALID_HANDLE_VALUE,
    _OPEN_EXISTING,
    _SDDL_REVISION_1,
    _advapi32,
    _close,
    _ConvertSddlToSd,
    _ConvertSidToStringSidW,
    _CreateFileW,
    _current_user_sid,
    _FileAttributeTagInfo,
    _GetFileInformationByHandleEx,
    _kernel32,
    _last_error,
    _link_refused,
    _LocalFree,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "ADMINISTRATORS_SID",
    "OWNER_RIGHTS_SID",
    "SYSTEM_SID",
    "AccessEntry",
    "SecuritySummary",
    "create_owner_only_folder",
    "current_user_sid",
    "fd_security",
    "folder_security",
    "hold_folder",
    "process_image",
    "release_folder",
    "restrict_folder_to_owner",
]

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"

_READ_CONTROL = 0x00020000
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_FILE_OBJECT = 1
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_DENIED_ACE_TYPE = 1
_INHERIT_ONLY_ACE = 0x08
_INHERITED_ACE = 0x10
_ERROR_ALREADY_EXISTS = 183
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _AclHeader(ctypes.Structure):
    _fields_ = [
        ("AclRevision", ctypes.c_ubyte),
        ("Sbz1", ctypes.c_ubyte),
        ("AclSize", ctypes.c_ushort),
        ("AceCount", ctypes.c_ushort),
        ("Sbz2", ctypes.c_ushort),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_ushort),
    ]


# ACCESS_ALLOWED_ACE and ACCESS_DENIED_ACE: the header, the access mask, then the SID.
_ACE_MASK_OFFSET = ctypes.sizeof(_AceHeader)
_ACE_SID_OFFSET = _ACE_MASK_OFFSET + ctypes.sizeof(wintypes.DWORD)

_CreateDirectoryW = _kernel32.CreateDirectoryW
_CreateDirectoryW.restype = wintypes.BOOL
_CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)]
_OpenProcess = _kernel32.OpenProcess
_OpenProcess.restype = wintypes.HANDLE
_OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_GetExitCodeProcess = _kernel32.GetExitCodeProcess
_GetExitCodeProcess.restype = wintypes.BOOL
_GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_QueryFullProcessImageNameW = _kernel32.QueryFullProcessImageNameW
_QueryFullProcessImageNameW.restype = wintypes.BOOL
_QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_GetSecurityInfo = _advapi32.GetSecurityInfo
_GetSecurityInfo.restype = wintypes.DWORD
_GetSecurityInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_void_p),
]
_SetNamedSecurityInfoW = _advapi32.SetNamedSecurityInfoW
_SetNamedSecurityInfoW.restype = wintypes.DWORD
_SetNamedSecurityInfoW.argtypes = [
    wintypes.LPWSTR,
    ctypes.c_int,
    wintypes.DWORD,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
_GetSecurityDescriptorDacl = _advapi32.GetSecurityDescriptorDacl
_GetSecurityDescriptorDacl.restype = wintypes.BOOL
_GetSecurityDescriptorDacl.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(wintypes.BOOL),
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.BOOL),
]
_GetAce = _advapi32.GetAce
_GetAce.restype = wintypes.BOOL
_GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]


@dataclass(frozen=True)
class AccessEntry:
    """One entry of a DACL: who, allow or deny, which rights, and how it got there."""

    sid: str
    allow: bool
    mask: int
    inherited: bool
    inherit_only: bool


@dataclass(frozen=True)
class SecuritySummary:
    """An object's owner SID and DACL. ``entries`` is ``None`` for a NULL DACL (all access).

    ``unknown_entries`` counts DACL entries of a type other than plain allow or deny
    (object or callback entries); a caller that cannot reason about them refuses.
    """

    owner: str
    entries: tuple[AccessEntry, ...] | None
    unknown_entries: int = 0


def current_user_sid() -> str:
    """The string SID of the user this process runs as (``TokenUser``)."""
    return _current_user_sid()


def _owner_only_sddl(*, inheritable: bool, with_owner: bool) -> str:
    sid = _current_user_sid()
    flags = "OICI" if inheritable else ""
    owner = f"O:{sid}" if with_owner else ""
    return f"{owner}D:P(A;{flags};FA;;;{sid})(A;{flags};FA;;;SY)"


def _descriptor(sddl: str) -> ctypes.c_void_p:
    descriptor = ctypes.c_void_p()
    if not _ConvertSddlToSd(sddl, _SDDL_REVISION_1, ctypes.byref(descriptor), None):
        raise _last_error("security descriptor")
    return descriptor


def create_owner_only_folder(folder: Path) -> bool:
    """Create ``folder`` owned by this user with the protected owner-and-SYSTEM DACL.

    The owner and DACL are applied atomically by ``CreateDirectoryW``; the entries are
    inheritable, so everything created inside gets them too. The parent must exist.

    Returns:
        ``True`` if the folder was created, ``False`` if something already had that name
        (its ownership and access are then the caller's to check).

    Raises:
        OSError: the folder could not be created for another reason.
    """
    descriptor = _descriptor(_owner_only_sddl(inheritable=True, with_owner=True))
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
        if _CreateDirectoryW(str(folder), ctypes.byref(attributes)):
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            return False
        raise _last_error(str(folder))
    finally:
        _LocalFree(descriptor)


def restrict_folder_to_owner(folder: Path) -> None:
    """Give ``folder`` a protected DACL for this user and SYSTEM only, inherited by all inside.

    The Windows reading of ``chmod 0700`` on a folder: nothing is inherited from the
    parent, and the two entries are inheritable (``OICI``), so every file and folder
    inside, existing or created later, is open only to the current user and the system
    account (``SetNamedSecurityInfoW`` propagates inheritable entries to the children).

    Raises:
        OSError: the security descriptor could not be built or applied.
    """
    descriptor = _descriptor(_owner_only_sddl(inheritable=True, with_owner=False))
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not _GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise _last_error(str(folder))
        code = _SetNamedSecurityInfoW(
            str(folder),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if code != 0:
            raise OSError(0, ctypes.FormatError(code).strip(), str(folder), code)
    finally:
        _LocalFree(descriptor)


def _sid_text(sid: int | None, filename: str) -> str:
    text = wintypes.LPWSTR()
    if not sid or not _ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise _last_error(filename)
    try:
        return str(text.value)
    finally:
        _LocalFree(ctypes.cast(text, ctypes.c_void_p))


def _entries(dacl: int, filename: str) -> tuple[tuple[AccessEntry, ...], int]:
    header = _AclHeader.from_address(dacl)
    entries: list[AccessEntry] = []
    unknown = 0
    for index in range(header.AceCount):
        ace = ctypes.c_void_p()
        if not _GetAce(dacl, index, ctypes.byref(ace)):
            raise _last_error(filename)
        address = int(ace.value or 0)
        ace_header = _AceHeader.from_address(address)
        if ace_header.AceType not in (_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_DENIED_ACE_TYPE):
            unknown += 1
            continue
        mask = wintypes.DWORD.from_address(address + _ACE_MASK_OFFSET).value
        entries.append(
            AccessEntry(
                sid=_sid_text(address + _ACE_SID_OFFSET, filename),
                allow=ace_header.AceType == _ACCESS_ALLOWED_ACE_TYPE,
                mask=int(mask),
                inherited=bool(ace_header.AceFlags & _INHERITED_ACE),
                inherit_only=bool(ace_header.AceFlags & _INHERIT_ONLY_ACE),
            )
        )
    return tuple(entries), unknown


def _handle_security(handle: int, filename: str) -> SecuritySummary:
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    code = _GetSecurityInfo(
        handle,
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if code != 0:
        raise OSError(0, ctypes.FormatError(code).strip(), filename, code)
    try:
        owner_sid = _sid_text(owner.value, filename)
        if not dacl.value:
            return SecuritySummary(owner=owner_sid, entries=None)
        entries, unknown = _entries(int(dacl.value), filename)
        return SecuritySummary(owner=owner_sid, entries=entries, unknown_entries=unknown)
    finally:
        _LocalFree(descriptor)


def folder_security(folder: Path) -> SecuritySummary:
    """The owner and DACL of ``folder`` itself, read by handle.

    Raises:
        WorkspaceLinkRefusedError: ``folder`` is a link, junction or other reparse point.
        NotADirectoryError: ``folder`` is not a folder.
        OSError: it could not be opened or read.
    """
    handle = _CreateFileW(
        str(folder),
        _READ_CONTROL | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        raise _last_error(str(folder))
    try:
        tag = _FileAttributeTagInfo()
        if not _GetFileInformationByHandleEx(
            handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(tag), ctypes.sizeof(tag)
        ):
            raise _last_error(str(folder))
        if tag.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise _link_refused(folder, "link_refused")
        if not tag.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise NotADirectoryError(errno.ENOTDIR, "not a folder", str(folder))
        return _handle_security(int(handle), str(folder))
    finally:
        _close(int(handle))


def fd_security(fd: int, filename: str) -> SecuritySummary:
    """The owner and DACL of the file open as ``fd`` (the handle needs READ_CONTROL)."""
    return _handle_security(msvcrt.get_osfhandle(fd), filename)


def process_image(pid: int) -> str | None:
    """The full executable path of the RUNNING process ``pid``, or ``None``.

    ``None`` when there is no such process, it has exited, or it cannot be queried.
    """
    handle = _OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not _GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != _STILL_ACTIVE:
            return None
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        _close(int(handle))


def hold_folder(folder: Path) -> int:
    """Open ``folder`` and return a handle that pins it and every folder above it.

    While the handle is open, Windows refuses to rename or move any folder on the path
    to ``folder`` (``ERROR_ACCESS_DENIED``), so a check made on that path stays true
    until :func:`release_folder`. The handle asks only for ``FILE_READ_ATTRIBUTES``
    (no ``DELETE``), does not follow a reparse point, and refuses one.

    Raises:
        WorkspaceLinkRefusedError: ``folder`` is a link, junction or other reparse point.
        NotADirectoryError: ``folder`` is not a folder.
        OSError: it could not be opened.
    """
    handle = _CreateFileW(
        str(folder),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_OPEN_REPARSE_POINT,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        raise _last_error(str(folder))
    try:
        tag = _FileAttributeTagInfo()
        if not _GetFileInformationByHandleEx(
            handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(tag), ctypes.sizeof(tag)
        ):
            raise _last_error(str(folder))
        if tag.FileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise _link_refused(folder, "link_refused")
        if not tag.FileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise NotADirectoryError(errno.ENOTDIR, "not a folder", str(folder))
    except BaseException:
        _close(int(handle))
        raise
    return int(handle)


def release_folder(handle: int) -> None:
    """Close a handle from :func:`hold_folder`."""
    _close(handle)
