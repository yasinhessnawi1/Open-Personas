"""The folder, delete and listing helpers of the sandbox opener (Spec WIN, T1.4).

POSIX keeps exactly the behaviour the callers had inline (``mkdir(parents=True)``,
``lstat`` then ``unlink``, ``rglob`` with ``is_file``); Windows refuses or skips every
link below the workspace root. Link cases on Windows use real junctions (no privilege
needed) and real symbolic links (Developer Mode or elevation; required in CI).
"""

from __future__ import annotations

import os
import stat
import sys
from typing import TYPE_CHECKING

import pytest
from persona.tools._sandbox import (
    delete_regular_file_nofollow,
    iter_regular_files_nofollow,
    make_dirs_nofollow,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the Windows branch")
BS = chr(92)


def _symlink(target: str | Path, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to create symbolic links: {exc}")
        pytest.skip(f"creating a symbolic link needs Developer Mode or elevation here: {exc}")


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """``(root, outside)``: a workspace and a sibling folder holding one file."""
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "dir" / "f.txt").write_bytes(b"OUTSIDE")
    root.mkdir()
    return root, outside


def _junction(target: Path, link: Path) -> None:
    if sys.platform != "win32":
        raise NotImplementedError("directory junctions exist only on Windows")
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


# --- every platform ----------------------------------------------------------------------
def test_make_dirs_creates_nested_folders_idempotently(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    make_dirs_nofollow(root / "a" / "b", root=root)
    make_dirs_nofollow(root / "a" / "b", root=root)
    assert (root / "a" / "b").is_dir()


def test_delete_reports_whether_a_regular_file_was_there(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    (root / "x.txt").write_bytes(b"x")
    (root / "d").mkdir()
    assert delete_regular_file_nofollow(root / "x.txt", root=root) is True
    assert not (root / "x.txt").exists()
    assert delete_regular_file_nofollow(root / "x.txt", root=root) is False
    assert delete_regular_file_nofollow(root / "d", root=root) is False
    assert (root / "d").is_dir()


def test_delete_leaves_a_symlink_and_its_target(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    _symlink(outside / "dir" / "f.txt", root / "link.txt")
    assert delete_regular_file_nofollow(root / "link.txt", root=root) is False
    assert (root / "link.txt").is_symlink()
    assert (outside / "dir" / "f.txt").read_bytes() == b"OUTSIDE"


def test_the_listing_yields_regular_files_with_their_sizes(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    (root / "a" / "b").mkdir(parents=True)
    (root / "top.txt").write_bytes(b"12345")
    (root / "a" / "b" / "deep.bin").write_bytes(b"xy")
    listed = {p.relative_to(root).as_posix(): size for p, size in iter_regular_files_nofollow(root)}
    assert listed == {"top.txt": 5, "a/b/deep.bin": 2}
    assert list(iter_regular_files_nofollow(root / "missing")) == []


# --- Windows -----------------------------------------------------------------------------
@windows_only
def test_make_dirs_through_a_junction_is_refused_and_creates_nothing_outside(
    layout: tuple[Path, Path],
) -> None:
    from persona.errors import WorkspaceLinkRefusedError

    root, outside = layout
    _junction(outside / "dir", root / "junc")
    with pytest.raises(WorkspaceLinkRefusedError):
        make_dirs_nofollow(root / "junc" / "made", root=root)
    assert sorted(p.name for p in (outside / "dir").iterdir()) == ["f.txt"]


@windows_only
def test_delete_through_a_junction_leaves_the_outside_file(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    _junction(outside / "dir", root / "junc")
    assert delete_regular_file_nofollow(root / "junc" / "f.txt", root=root) is False
    assert (outside / "dir" / "f.txt").read_bytes() == b"OUTSIDE"


@windows_only
def test_the_listing_never_enters_or_reports_a_link(layout: tuple[Path, Path]) -> None:
    """``rglob`` on Windows descends into junctions and reports a symbolic link to an
    outside file as a file; the listing must do neither."""
    root, outside = layout
    (root / "own.txt").write_bytes(b"mine")
    _junction(outside / "dir", root / "junc")
    _symlink(outside / "dir", root / "dirlink", directory=True)
    _symlink(outside / "dir" / "f.txt", root / "filelink.txt")
    listed = {p.relative_to(root).as_posix() for p, _ in iter_regular_files_nofollow(root)}
    assert listed == {"own.txt"}
    assert list(iter_regular_files_nofollow(root / "junc")) == []


@windows_only
def test_the_listing_never_contacts_a_network_share_behind_a_link(
    layout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import nt

    root, _ = layout
    host = "127.0.0.1"
    _symlink(f"{BS}{BS}{host}{BS}nosuchshare$", root / "share", directory=True)
    (root / "own.txt").write_bytes(b"mine")
    real_final, real_stat = vars(nt)["_getfinalpathname"], os.stat

    def guarded(original: Callable[..., object]) -> Callable[..., object]:
        def call(path: object, *args: object, **kwargs: object) -> object:
            if host in str(path):
                raise AssertionError(f"asked the system about {path!r}")
            return original(path, *args, **kwargs)

        return call

    monkeypatch.setattr(nt, "_getfinalpathname", guarded(real_final))
    monkeypatch.setattr(os, "stat", guarded(real_stat))
    listed = {p.name for p, _ in iter_regular_files_nofollow(root)}
    assert listed == {"own.txt"}


def test_a_read_only_file_is_deleted_as_posix_unlink_does(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    target = root / "locked.txt"
    target.write_bytes(b"x")
    target.chmod(stat.S_IREAD)
    try:
        assert delete_regular_file_nofollow(target, root=root) is True
        assert not target.exists()
    finally:
        if target.exists():
            target.chmod(stat.S_IREAD | stat.S_IWRITE)


@windows_only
def test_the_windows_delete_goes_through_the_handle_never_a_path_unlink(
    layout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic proof (T1.5 L-5): with every path-based unlink made to raise, the
    delete still succeeds (it deletes through the no-follow handle) and still refuses a
    junction in the path."""
    from pathlib import Path as RealPath

    root, outside = layout
    (root / "plain.txt").write_bytes(b"x")
    _junction(outside / "dir", root / "junc")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a path-based unlink was used")

    monkeypatch.setattr(os, "unlink", forbidden)
    monkeypatch.setattr(os, "remove", forbidden)
    monkeypatch.setattr(RealPath, "unlink", forbidden)
    assert delete_regular_file_nofollow(root / "plain.txt", root=root) is True
    assert delete_regular_file_nofollow(root / "junc" / "f.txt", root=root) is False
    monkeypatch.undo()
    assert not (root / "plain.txt").exists()
    assert (outside / "dir" / "f.txt").read_bytes() == b"OUTSIDE"


def test_write_file_under_root_refuses_a_destination_outside_the_root(
    layout: tuple[Path, Path],
) -> None:
    from persona.tools._sandbox import write_file_under_root

    root, outside = layout
    with pytest.raises(ValueError, match="not inside the workspace root"):
        write_file_under_root(outside / "new.txt", b"X", root=root)
    assert not (outside / "new.txt").exists()
    write_file_under_root(root / "a" / "b" / "new.txt", b"ok", root=root)
    assert (root / "a" / "b" / "new.txt").read_bytes() == b"ok"


def test_write_file_under_root_refuses_any_dotdot_part(layout: tuple[Path, Path]) -> None:
    """``is_relative_to`` is lexical and does not collapse ``..``: ``root/../x`` passes it
    (T1.6 L-B), so any ``..`` part is refused before the check."""
    from persona.tools._sandbox import write_file_under_root

    root, _ = layout
    for escape in (root / ".." / "x.txt", root / "a" / ".." / ".." / "x.txt"):
        with pytest.raises(ValueError, match="not inside the workspace root"):
            write_file_under_root(escape, b"X", root=root)
    assert not (root.parent / "x.txt").exists()
