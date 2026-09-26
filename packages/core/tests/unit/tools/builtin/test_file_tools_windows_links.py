"""The file tools on Windows meet links (Spec WIN, T1.4, rulings 3 and 5).

A refusal reaches the model as a clean, human message with no host path and no raw
exception text; the AttributeError that used to escape (``os.O_NOFOLLOW``) is gone.
A link in the path is refused by the resolver (reason ``link``); a hard-linked file,
which is not a link in the path, is refused by the opener.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

if sys.platform != "win32":
    pytest.skip("links on Windows", allow_module_level=True)

import _winapi

from persona.tools.builtin.file_read import make_file_read_tool
from persona.tools.builtin.file_write import make_file_write_tool

if TYPE_CHECKING:
    from pathlib import Path

LINK_MESSAGE = "SandboxViolationError: path goes through a link or shortcut [reason=link]; "
LINK_HINT = "The workspace does not follow links or shortcuts"
HARD_LINK_MESSAGE = "WorkspaceLinkRefusedError: This file also exists under another name"
NOISE = ("AttributeError", "O_NOFOLLOW", "WinError", "NTSTATUS", "Errno", "Traceback")

pytestmark = pytest.mark.asyncio


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """``(root, outside)``; ``root/in_junc`` is a junction to ``root/real`` (inside)."""
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    (root / "real").mkdir(parents=True)
    (root / "real" / "f.txt").write_bytes(b"INSIDE")
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"OUTSIDE")
    _winapi.CreateJunction(str(root / "real"), str(root / "in_junc"))
    return root, outside


def _assert_clean(content: str, expected_start: str, tmp_path: Path) -> None:
    assert content.startswith(expected_start), content
    assert str(tmp_path) not in content  # no host path reaches the model
    for noise in NOISE:
        assert noise not in content, content


async def test_file_read_through_a_link_gets_the_human_refusal(
    layout: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _ = layout
    result = await make_file_read_tool(sandbox_root=root).execute(path="in_junc/f.txt")
    assert result.is_error
    _assert_clean(result.content, LINK_MESSAGE, tmp_path)
    assert LINK_HINT in result.content


async def test_file_write_through_a_link_is_refused_and_writes_nothing(
    layout: tuple[Path, Path], tmp_path: Path
) -> None:
    root, _ = layout
    tool = make_file_write_tool(sandbox_root=root)
    for path in ("in_junc/f.txt", "in_junc/new.txt", "in_junc/sub/new.txt"):
        result = await tool.execute(path=path, content="X")
        assert result.is_error, path
        _assert_clean(result.content, LINK_MESSAGE, tmp_path)
        assert LINK_HINT in result.content
    assert sorted(p.name for p in (root / "real").iterdir()) == ["f.txt"]
    assert (root / "real" / "f.txt").read_bytes() == b"INSIDE"


async def test_a_hard_link_to_an_outside_file_is_refused_for_read_and_write(
    layout: tuple[Path, Path], tmp_path: Path
) -> None:
    root, outside = layout
    os.link(outside / "secret.txt", root / "hard.txt")
    read = await make_file_read_tool(sandbox_root=root).execute(path="hard.txt")
    _assert_clean(read.content, HARD_LINK_MESSAGE, tmp_path)
    write = await make_file_write_tool(sandbox_root=root).execute(path="hard.txt", content="X")
    _assert_clean(write.content, HARD_LINK_MESSAGE, tmp_path)
    assert (outside / "secret.txt").read_bytes() == b"OUTSIDE"


async def test_plain_files_and_new_folders_still_work(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    written = await make_file_write_tool(sandbox_root=root).execute(
        path="out/deep/report.md", content="hello\nworld\n"
    )
    assert not written.is_error, written.content
    assert (root / "out" / "deep" / "report.md").read_bytes() == b"hello\nworld\n"
    read = await make_file_read_tool(sandbox_root=root).execute(path="out/deep/report.md")
    assert read.content == "hello\nworld\n"
