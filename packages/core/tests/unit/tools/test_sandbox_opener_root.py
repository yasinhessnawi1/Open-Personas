"""The sandbox opener's ``root`` argument (Spec WIN, T1.2).

On Windows the public opener dispatches to :mod:`persona.tools._winopen` and works
under ``root``: a link in ANY component below it is refused. With ``root=None`` the
file's own folder is the root, which gives only the final-component guarantee (the
POSIX ``O_NOFOLLOW`` one). On POSIX ``root`` is ignored and behaviour is unchanged.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from persona.tools._sandbox import (
    is_regular_file_nofollow,
    open_nofollow,
    read_nofollow_bytes,
    write_nofollow_bytes,
)

if TYPE_CHECKING:
    from pathlib import Path

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the Windows dispatch")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the POSIX path")

SECRET = b"OUTSIDE SECRET"


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """``(root, outside)`` with a junction ``root/junc`` pointing at ``outside/dir``."""
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    root.mkdir()
    (outside / "dir" / "f.txt").write_bytes(SECRET)
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(outside / "dir"), str(root / "junc"))
    return root, outside


@windows_only
def test_with_the_root_a_junction_mid_path_is_refused_for_read_write_and_probe(
    layout: tuple[Path, Path],
) -> None:
    from persona.errors import WorkspaceLinkRefusedError

    root, outside = layout
    target = root / "junc" / "f.txt"
    with pytest.raises(WorkspaceLinkRefusedError) as info:
        read_nofollow_bytes(target, root=root)
    assert "does not follow links or shortcuts" in str(info.value)
    with pytest.raises(WorkspaceLinkRefusedError):
        write_nofollow_bytes(target, b"X", root=root)
    assert is_regular_file_nofollow(target, root=root) is False
    assert (outside / "dir" / "f.txt").read_bytes() == SECRET


@windows_only
def test_with_the_root_a_hard_link_is_refused(layout: tuple[Path, Path]) -> None:
    from persona.errors import WorkspaceLinkRefusedError

    root, outside = layout
    os.link(outside / "dir" / "f.txt", root / "hard.txt")
    with pytest.raises(WorkspaceLinkRefusedError):
        read_nofollow_bytes(root / "hard.txt", root=root)
    assert is_regular_file_nofollow(root / "hard.txt", root=root) is False


@windows_only
def test_bytes_round_trip_exactly_through_the_public_api(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    payload = bytes(range(256)) + b"a\nb\r\nc\x1ad"
    write_nofollow_bytes(root / "sub.bin", payload, root=root)
    assert read_nofollow_bytes(root / "sub.bin", root=root) == payload
    assert (root / "sub.bin").read_bytes() == payload


@windows_only
def test_the_root_is_the_callers_or_else_the_files_own_folder(
    layout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = layout
    seen: list[tuple[Path, Path]] = []

    def spy(r: Path, p: Path, _flags: int) -> int:
        seen.append((r, p))
        return os.open(os.devnull, os.O_RDONLY)

    from persona.tools import _winopen

    monkeypatch.setattr(_winopen, "open_fd", spy)
    os.close(open_nofollow(root / "a" / "b.txt", os.O_RDONLY, root=root))
    os.close(open_nofollow(root / "a" / "b.txt", os.O_RDONLY, final_component_only=True))
    assert seen == [(root.resolve(), root / "a" / "b.txt"), (root / "a", root / "a" / "b.txt")]


@windows_only
def test_without_a_root_the_windows_opener_refuses_unless_the_caller_opts_in(
    layout: tuple[Path, Path],
) -> None:
    """Fail secure (T1.5 L-3): forgetting root= never silently gives the weaker guarantee."""
    root, _ = layout
    (root / "a.txt").write_bytes(b"x")
    for call in (
        lambda: read_nofollow_bytes(root / "a.txt"),
        lambda: write_nofollow_bytes(root / "a.txt", b"y"),
        lambda: is_regular_file_nofollow(root / "a.txt"),
        lambda: open_nofollow(root / "a.txt", os.O_RDONLY),
    ):
        with pytest.raises(ValueError, match="needs the workspace root"):
            call()
    assert (root / "a.txt").read_bytes() == b"x"
    assert read_nofollow_bytes(root / "a.txt", final_component_only=True) == b"x"


@windows_only
def test_a_relative_root_is_resolved_against_the_working_directory(
    layout: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = layout
    monkeypatch.chdir(root.parent)
    rel_root = type(root)("workspace")
    write_nofollow_bytes(root / "r.txt", b"ok", root=rel_root)
    assert read_nofollow_bytes(root / "r.txt", root=rel_root) == b"ok"


@posix_only
def test_on_posix_the_root_is_ignored(tmp_path: Path) -> None:
    """POSIX stays byte-identical: ``root`` changes nothing, even an unrelated one."""
    target = tmp_path / "a.txt"
    unrelated = tmp_path / "elsewhere"
    write_nofollow_bytes(target, b"posix", root=unrelated)
    assert read_nofollow_bytes(target, root=unrelated) == b"posix"
    assert is_regular_file_nofollow(target, root=unrelated) is True
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(OSError):  # noqa: PT011 - the kernel's ELOOP, unchanged
        read_nofollow_bytes(link, root=unrelated)
