"""Local file storage on Windows meets links (Spec WIN, T1.4).

The default storage backend resolves every key (a link in the key's path is refused
by the resolver, reason ``link``) and then puts, gets, streams, probes, deletes and
lists through the no-follow helpers with the workspace root (a hard-linked file is
refused by the opener; the listing never enters a link it meets inside a folder).
Nothing outside the workspace, and nothing behind a link, is touched.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

if sys.platform != "win32":
    pytest.skip("links on Windows", allow_module_level=True)

import _winapi

from persona.errors import SandboxViolationError, WorkspaceLinkRefusedError
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """``(root, outside)``; ``root/in_junc`` is a junction to ``root/real`` (inside), and
    ``root/hard.txt`` is a hard link to ``outside/secret.txt``."""
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    (root / "real").mkdir(parents=True)
    (root / "real" / "f.txt").write_bytes(b"INSIDE")
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"OUTSIDE")
    _winapi.CreateJunction(str(root / "real"), str(root / "in_junc"))
    os.link(outside / "secret.txt", root / "hard.txt")
    return root, outside


def test_put_through_a_link_is_refused_and_writes_nothing(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    storage = LocalFileStorage(root)
    for key in ("in_junc/new.bin", "in_junc/sub/new.bin", "in_junc/f.txt"):
        with pytest.raises(SandboxViolationError) as info:
            storage.put(key, b"X")
        assert info.value.context["reason"] == "link"
    with pytest.raises(WorkspaceLinkRefusedError):
        storage.put("hard.txt", b"X")
    assert sorted(p.name for p in (root / "real").iterdir()) == ["f.txt"]


def test_get_stream_and_exists_refuse_links_and_hard_links(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    storage = LocalFileStorage(root)
    with pytest.raises(SandboxViolationError):
        storage.get("in_junc/f.txt")
    with pytest.raises(SandboxViolationError):
        storage.exists("in_junc/f.txt")
    with pytest.raises(WorkspaceLinkRefusedError):
        storage.get("hard.txt")
    with pytest.raises(WorkspaceLinkRefusedError):
        b"".join(storage.open_stream("hard.txt"))
    assert storage.exists("hard.txt") is False
    assert storage.get("real/f.txt") == b"INSIDE"


def test_delete_through_a_link_or_of_a_hard_link_leaves_every_file(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    storage = LocalFileStorage(root)
    with pytest.raises(SandboxViolationError):
        storage.delete("in_junc/f.txt")
    assert storage.delete("hard.txt") is False
    assert (root / "real" / "f.txt").read_bytes() == b"INSIDE"
    assert (outside / "secret.txt").read_bytes() == b"OUTSIDE"


def test_the_listing_skips_links_and_reports_only_the_real_files(
    layout: tuple[Path, Path],
) -> None:
    root, _ = layout
    storage = LocalFileStorage(root)
    storage.put("docs/a.txt", b"abc")
    keys = {obj.key: obj.size for obj in storage.list("docs")}
    assert keys == {"docs/a.txt": 3}
    everything = {obj.key for obj in storage.list("real")}
    assert everything == {"real/f.txt"}
    with pytest.raises(SandboxViolationError):
        list(storage.list("in_junc"))
    (root / "docs" / "sub").mkdir()
    import _winapi as winapi

    winapi.CreateJunction(str(root / "real"), str(root / "docs" / "sub" / "loop"))
    assert {obj.key for obj in storage.list("docs")} == {"docs/a.txt"}
