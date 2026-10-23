"""The bundled database's folders and private files on a real Windows file system (review M-1).

Real folders, real DACLs (set with ``icacls``, a Windows builtin), a real junction. A
second Windows account is not available on this machine or in CI, so a folder or file
OWNED BY ANOTHER ACCOUNT is simulated with a test double of the owner read; everything
else is the real thing.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

import pytest
from persona_api.db import _managed_files as mf
from persona_api.db import _managed_windows as mw
from persona_api.errors import CommunityDbError

if sys.platform != "win32":
    pytest.skip("Windows DACLs only", allow_module_level=True)

from persona.tools import _winacl  # noqa: E402 - Windows-only, after the platform skip

USERS = "S-1-5-32-545"


def _prepare(datadir: Path) -> None:
    with mf.prepared_folders(datadir):
        pass


ANOTHER_ACCOUNT = "S-1-5-21-1-2-3-4242"


def _icacls(*args: str) -> None:
    subprocess.run(["icacls", *args], capture_output=True, check=True, timeout=30)  # noqa: S607


def _grants(folder: Path) -> list[tuple[str, bool]]:
    entries = _winacl.folder_security(folder).entries or ()
    return sorted((e.sid, e.inherited) for e in entries if e.allow)


def test_a_fresh_install_creates_both_folders_owner_only(tmp_path: Path) -> None:
    datadir = tmp_path / "base" / "pg16"
    _prepare(datadir)
    me = _winacl.current_user_sid()
    for folder in (datadir.parent, datadir):
        summary = _winacl.folder_security(folder)
        assert summary.owner == me
        assert _grants(folder) == sorted([(me, False), ("S-1-5-18", False)])


def test_an_older_builds_data_folder_is_accepted_and_limited_to_this_account(
    tmp_path: Path,
) -> None:
    datadir = tmp_path / "pg16"
    datadir.mkdir()  # inherits its parent's entries, as a pgserver data folder did
    (datadir / "PG_VERSION").write_text("16\n", encoding="utf-8")
    assert any(inherited for _, inherited in _grants(datadir))
    _prepare(datadir)
    me = _winacl.current_user_sid()
    assert _grants(datadir) == sorted([(me, False), ("S-1-5-18", False)])
    # The file inside now carries only the two inherited entries.
    child = subprocess.run(
        ["icacls", str(datadir / "PG_VERSION")], capture_output=True, text=True, check=True
    ).stdout
    assert "BUILTIN\\Administrators" not in child


def test_a_data_folder_granting_another_account_is_refused(tmp_path: Path) -> None:
    datadir = tmp_path / "pg16"
    datadir.mkdir()
    _icacls(str(datadir), "/grant", f"*{USERS}:(OI)(CI)(RX)")
    with pytest.raises(CommunityDbError) as caught:
        _prepare(datadir)
    assert caught.value.context["reason"] == "unsafe_data_folder"
    assert str(datadir) in str(caught.value)
    assert USERS in [sid for sid, _ in _grants(datadir)]  # refused, not silently rewritten


def test_a_base_folder_where_others_can_create_folders_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "shared"
    base.mkdir()
    _icacls(str(base), "/grant", f"*{USERS}:(OI)(CI)(M)")
    with pytest.raises(CommunityDbError) as caught:
        _prepare(base / "pg16")
    assert caught.value.context["reason"] == "unsafe_base_folder"
    assert not (base / "pg16").exists()


def test_a_data_folder_that_is_a_junction_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    datadir = tmp_path / "pg16"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(datadir), str(elsewhere)],  # noqa: S607
        capture_output=True,
        check=True,
    )
    with pytest.raises(OSError, match="(?i)link"):
        _prepare(datadir)


def test_a_data_folder_owned_by_another_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test double of the owner read: the folder is real, its owner is another account."""
    datadir = tmp_path / "pg16"
    datadir.mkdir()
    real = _winacl.folder_security

    def owned_by_another(folder: Path) -> _winacl.SecuritySummary:
        summary = real(folder)
        if folder == datadir:
            return _winacl.SecuritySummary(ANOTHER_ACCOUNT, summary.entries)
        return summary

    monkeypatch.setattr(_winacl, "folder_security", owned_by_another)
    with pytest.raises(CommunityDbError) as caught:
        _prepare(datadir)
    assert caught.value.context["reason"] == "unsafe_data_folder"
    assert ANOTHER_ACCOUNT in str(caught.value)


def test_a_password_file_owned_by_another_account_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test double of the owner read, through the handle the file is read from."""
    passfile = tmp_path / "pg16.pgpass"
    mf.create_private_file(passfile, b"*:*:*:postgres:x\n")
    real = _winacl.fd_security

    def owned_by_another(fd: int, filename: str) -> _winacl.SecuritySummary:
        return _winacl.SecuritySummary(ANOTHER_ACCOUNT, real(fd, filename).entries)

    monkeypatch.setattr(_winacl, "fd_security", owned_by_another)
    with pytest.raises(CommunityDbError) as caught:
        mf.read_private_file(passfile)
    assert caught.value.context["reason"] == "unsafe_private_file"


def test_a_created_private_file_is_owner_only(tmp_path: Path) -> None:
    passfile = tmp_path / "pg16.pgpass"
    mf.create_private_file(passfile, b"secret\n")
    assert mf.read_private_file(passfile) == b"secret\n"


def test_a_planted_staged_initdb_password_is_refused(tmp_path: Path) -> None:
    datadir = tmp_path / "pg16"
    _prepare(datadir)
    staged = datadir.with_name("pg16.initpw")
    staged.write_text("planted\n", encoding="utf-8")  # inherits the folder's entries
    with pytest.raises(CommunityDbError) as caught:
        mw._initdb(tmp_path / "bin", datadir, "the-password")
    assert caught.value.context["reason"] == "unsafe_private_file"
    assert staged.read_text(encoding="utf-8") == "planted\n"


def _short_path(path: Path) -> str:
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value


def test_a_base_folder_given_by_its_8_3_short_name_is_resolved_once(tmp_path: Path) -> None:
    """Review L-3: the no-follow opener compares the file's path with its folder's."""
    from persona_api.db.community_managed import CommunityDbManager

    long_base = tmp_path / "a_rather_long_folder_name"
    long_base.mkdir()
    short = _short_path(long_base)
    if short.lower() == str(long_base).lower():
        pytest.skip("8.3 short names are disabled on this volume")
    manager = CommunityDbManager(external_url=None, base_dir=Path(short))
    assert manager._base_dir == long_base.resolve()  # noqa: SLF001 - the resolved base


def test_both_folders_are_made_by_the_atomic_owner_only_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DACL must exist from the first moment, not be applied after a plain mkdir."""
    made: list[Path] = []
    real = _winacl.create_owner_only_folder

    def recording(folder: Path) -> bool:
        made.append(folder)
        return real(folder)

    monkeypatch.setattr(_winacl, "create_owner_only_folder", recording)
    datadir = tmp_path / "base" / "pg16"
    _prepare(datadir)
    assert made == [datadir.parent, datadir]


def test_a_data_folder_that_could_not_be_limited_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    datadir = tmp_path / "pg16"
    datadir.mkdir()  # inherited entries only
    monkeypatch.setattr(_winacl, "restrict_folder_to_owner", lambda _folder: None)
    with pytest.raises(CommunityDbError) as caught:
        _prepare(datadir)
    assert caught.value.context["reason"] == "unsafe_data_folder"


def test_no_folder_above_the_data_folder_can_be_renamed_while_it_is_held(
    tmp_path: Path,
) -> None:
    """Review N-1: a folder that passed the checks cannot be swapped before the start."""
    ancestor = tmp_path / "shared"
    datadir = ancestor / "persona" / "pg16"
    with mf.prepared_folders(datadir):
        for folder in (ancestor, datadir.parent):
            with pytest.raises(PermissionError):
                folder.rename(folder.with_name(folder.name + "-aside"))
    moved = ancestor.rename(ancestor.with_name("shared-aside"))  # released afterwards
    assert moved.is_dir()


def test_every_missing_folder_on_the_way_is_created_owner_only(tmp_path: Path) -> None:
    """Review N-1: missing ancestors are created with the atomic owner-only create."""
    top = tmp_path / "a"
    datadir = top / "b" / "base" / "pg16"
    _prepare(datadir)
    me = _winacl.current_user_sid()
    for folder in (top, top / "b", datadir.parent, datadir):
        summary = _winacl.folder_security(folder)
        assert summary.owner == me, folder
        assert _grants(folder) == sorted([(me, False), ("S-1-5-18", False)]), folder


def test_the_folders_are_checked_while_already_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handle is opened before the checks, so nothing can move between them."""
    events: list[str] = []
    real_hold = _winacl.hold_folder
    real_read = _winacl.folder_security

    def hold(folder: Path) -> int:
        events.append("hold")
        return real_hold(folder)

    def read(folder: Path) -> _winacl.SecuritySummary:
        events.append("check")
        return real_read(folder)

    monkeypatch.setattr(_winacl, "hold_folder", hold)
    monkeypatch.setattr(_winacl, "folder_security", read)
    _prepare(tmp_path / "base" / "pg16")
    assert events == ["hold", "check", "hold", "check", "check"]  # each held, then checked


def test_a_created_file_is_owned_by_this_user(tmp_path: Path) -> None:
    """Every object the product creates is user-owned, files as well as folders."""
    passfile = tmp_path / "pg16.pgpass"
    mf.create_private_file(passfile, b"x\n")
    import os

    from persona.tools._sandbox import open_nofollow

    fd = open_nofollow(passfile, os.O_RDONLY, root=tmp_path)
    try:
        assert _winacl.fd_security(fd, str(passfile)).owner == _winacl.current_user_sid()
    finally:
        os.close(fd)
