"""Who may own and open the bundled database's folders on Windows (Spec WIN, T2 review M-1).

POSIX Postgres refuses a data folder it does not own or that others can open
(``checkDataDir``). That check is compiled out on Windows, and the owner of a folder
can always rewrite its DACL: an account that pre-created ``pg16`` in a folder where
others may create folders could re-grant itself access, replace
``postgresql.auto.conf`` and run code as the victim at the next start. These rules
supply the missing check. They are pure functions over an owner-and-DACL summary
(:class:`persona.tools._winacl.SecuritySummary` or a test double), so they are tested
on every platform.

Trusted owners are the user this process runs as, SYSTEM and the Administrators
group. An object owned by Administrators can only be changed by an elevated
administrator, who can already act as SYSTEM and as any user on the machine, so that
ownership gives nobody a capability they lack; and a process started elevated creates
objects owned by Administrators, which must keep working when the same user later runs
unelevated. Any other owner is refused.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "base_folder_refusal",
    "datadir_refusal",
    "private_file_refusal",
    "restricted_datadir_refusal",
]

_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"
_OWNER_RIGHTS = "S-1-3-4"

#: Rights that let a holder add, replace, delete or re-permission what is in a folder.
_WRITE_RIGHTS = (
    0x0002  # FILE_ADD_FILE
    | 0x0004  # FILE_ADD_SUBDIRECTORY
    | 0x0040  # FILE_DELETE_CHILD
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)


class _Entry(Protocol):
    @property
    def sid(self) -> str: ...
    @property
    def allow(self) -> bool: ...
    @property
    def mask(self) -> int: ...
    @property
    def inherited(self) -> bool: ...
    @property
    def inherit_only(self) -> bool: ...


class _Summary(Protocol):
    @property
    def owner(self) -> str: ...
    @property
    def entries(self) -> Sequence[_Entry] | None: ...
    @property
    def unknown_entries(self) -> int: ...


def _owner_refusal(summary: _Summary, user_sid: str, path: Path, what: str) -> str | None:
    if summary.owner in (user_sid, _SYSTEM, _ADMINISTRATORS):
        return None
    return (
        f"{what} {path} belongs to another account ({summary.owner}), so the bundled database "
        "will not use it: its owner could change it. Delete it, or set "
        "PERSONA_API_COMMUNITY_MANAGED_DB_DIR to a folder in your own user profile."
    )


def _unreadable_dacl(summary: _Summary, path: Path, what: str) -> str | None:
    if summary.entries is None:
        return (
            f"{what} {path} has no access list, so every account can open it. Delete it, or "
            "set PERSONA_API_COMMUNITY_MANAGED_DB_DIR to a folder in your own user profile."
        )
    if summary.unknown_entries:
        return (
            f"{what} {path} has access entries this check cannot read. Delete it, or set "
            "PERSONA_API_COMMUNITY_MANAGED_DB_DIR to a folder in your own user profile."
        )
    return None


def base_folder_refusal(summary: _Summary, user_sid: str, folder: Path) -> str | None:
    """Why the folder holding the data folder and password file is unsafe, or ``None``.

    It must be owned by a trusted owner, and no one else may add, replace or delete what
    is in it. Read access for others is fine: what is inside is owner-only.
    """
    what = "The database folder"
    refusal = _owner_refusal(summary, user_sid, folder, what) or _unreadable_dacl(
        summary, folder, what
    )
    if refusal:
        return refusal
    trusted = {user_sid, _SYSTEM, _ADMINISTRATORS, _OWNER_RIGHTS}
    for entry in summary.entries or ():
        grants_write = entry.allow and not entry.inherit_only and entry.mask & _WRITE_RIGHTS
        if grants_write and entry.sid not in trusted:
            return (
                f"The database folder {folder} lets another account ({entry.sid}) create "
                "or replace files in it, so the bundled database will not keep its data "
                "there. Set PERSONA_API_COMMUNITY_MANAGED_DB_DIR to a folder in your own "
                "user profile (the default is ~/.persona)."
            )
    return None


def datadir_refusal(summary: _Summary, user_sid: str, folder: Path) -> str | None:
    """Why an existing data folder must not be used, or ``None``.

    Owned by a trusted owner, and no EXPLICIT entry granting anyone else: an explicit
    grant was put there by someone. Inherited entries are replaced by the owner-only
    DACL (a data folder made by an older build inherited its parent's entries).
    """
    what = "The data folder"
    refusal = _owner_refusal(summary, user_sid, folder, what) or _unreadable_dacl(
        summary, folder, what
    )
    if refusal:
        return refusal
    for entry in summary.entries or ():
        if entry.allow and not entry.inherited and entry.sid not in (user_sid, _SYSTEM):
            return (
                f"The data folder {folder} grants another account ({entry.sid}) access, which "
                "the bundled database never sets. Something changed it; check who, then "
                "remove that entry or move the folder aside to start with a fresh database."
            )
    return None


def restricted_datadir_refusal(summary: _Summary, user_sid: str, folder: Path) -> str | None:
    """After the owner-only DACL is applied: exactly this user and SYSTEM, nothing inherited."""
    what = "The data folder"
    refusal = _unreadable_dacl(summary, folder, what)
    if refusal:
        return refusal
    for entry in summary.entries or ():
        if entry.allow and (entry.inherited or entry.sid not in (user_sid, _SYSTEM)):
            return (
                f"The data folder {folder} could not be limited to your account: "
                f"{entry.sid} still has access. Check the folder's permissions."
            )
    return None


def private_file_refusal(summary: _Summary, user_sid: str, path: Path) -> str | None:
    """Why the password file (or the staged initdb password) must not be used, or ``None``.

    The product creates these files itself with an owner-only DACL, so anything else
    (another owner, or any other account granted access) means someone else made or
    changed it.
    """
    what = "The password file"
    refusal = _owner_refusal(summary, user_sid, path, what) or _unreadable_dacl(summary, path, what)
    if refusal:
        return refusal
    for entry in summary.entries or ():
        if entry.allow and entry.sid not in (user_sid, _SYSTEM):
            return (
                f"The password file {path} can be opened by another account ({entry.sid}), "
                "so the bundled database will not trust it. The product never creates it "
                "that way: delete it only if you are sure no database needs it, or move the "
                "data folder aside to start fresh."
            )
    return None
