"""The local sandbox's output folder is read without following links (Spec WIN T1.6, X-1).

Code in the container controls ``/workspace/out``, which is a host folder: a symlink
``out/leak.txt -> <host secret>`` must never be listed or copied, a folder swapped for a
link after the listing must not redirect the read, and a hard link or a FIFO is refused.
Runs on every platform (the Linux run is the one that matters for this backend).
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from persona.sandbox._produced_io import list_produced_files, read_produced_file
from persona.sandbox.errors import (
    CodeSandboxError,
    ProducedFileRefusedError,
    ProducedFileSizeError,
)
from persona.sandbox.local_docker import LocalDockerSandbox
from persona.sandbox.result import ResourceLimits

if TYPE_CHECKING:
    from pathlib import Path

SECRET = b"PERSONA_SECRET=do-not-leak"
CAP = 1 << 20


def _symlink(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to create symbolic links: {exc}")
        pytest.skip(f"creating a symbolic link needs Developer Mode or elevation here: {exc}")


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(host_out, secret_dir, workspace)``: a host secret sits outside the out folder."""
    host_out, secret_dir, workspace = tmp_path / "out", tmp_path / "srv", tmp_path / "ws"
    host_out.mkdir()
    secret_dir.mkdir()
    workspace.mkdir()
    (secret_dir / ".env").write_bytes(SECRET)
    (host_out / "chart.png").write_bytes(b"\x89PNG")
    return host_out, secret_dir, workspace


def test_a_symlink_in_out_is_neither_listed_nor_copied(
    layout: tuple[Path, Path, Path],
) -> None:
    host_out, secret_dir, workspace = layout
    _symlink(secret_dir / ".env", host_out / "leak.txt")
    assert [name for name, _ in list_produced_files(host_out)] == ["chart.png"]
    with pytest.raises(ProducedFileRefusedError):
        LocalDockerSandbox._copy_produced_sync(  # noqa: SLF001
            host_out, workspace / "uploads" / "leak.txt", "s-1", "leak.txt", workspace
        )
    assert not (workspace / "uploads" / "leak.txt").exists()


def test_discovery_reports_only_regular_files_and_never_enters_a_linked_folder(
    layout: tuple[Path, Path, Path],
) -> None:
    host_out, secret_dir, _ = layout
    (host_out / "charts").mkdir()
    (host_out / "charts" / "sales.png").write_bytes(b"x")
    _symlink(secret_dir, host_out / "linked", directory=True)
    produced, _, refused = LocalDockerSandbox._discover_produced_files(  # noqa: SLF001
        host_out, ResourceLimits()
    )
    assert [f.path for f in produced] == ["chart.png", "charts/sales.png"]
    assert refused == ()


def test_a_folder_swapped_for_a_link_after_the_listing_does_not_redirect_the_read(
    layout: tuple[Path, Path, Path],
) -> None:
    """A background process in the container can swap a folder after discovery."""
    host_out, secret_dir, _ = layout
    (host_out / "d").mkdir()
    (host_out / "d" / ".env").write_bytes(b"INSIDE")
    assert "d/.env" in [name for name, _ in list_produced_files(host_out)]
    (host_out / "d").rename(host_out / "parked")
    _symlink(secret_dir, host_out / "d", directory=True)
    with pytest.raises(ProducedFileRefusedError):
        read_produced_file(host_out, "d/.env", session_id="s-1", cap_bytes=CAP)


def test_a_hard_link_to_a_host_file_is_refused(layout: tuple[Path, Path, Path]) -> None:
    host_out, secret_dir, _ = layout
    os.link(secret_dir / ".env", host_out / "hard.txt")
    with pytest.raises(ProducedFileRefusedError):
        read_produced_file(host_out, "hard.txt", session_id="s-1", cap_bytes=CAP)


def test_a_fifo_is_not_listed_and_its_read_is_refused_without_hanging(
    layout: tuple[Path, Path, Path],
) -> None:
    if sys.platform == "win32":
        pytest.skip("FIFOs exist only on POSIX")
    host_out, _, _ = layout
    os.mkfifo(host_out / "pipe")
    assert "pipe" not in [name for name, _ in list_produced_files(host_out)]
    with pytest.raises(ProducedFileRefusedError):
        read_produced_file(host_out, "pipe", session_id="s-1", cap_bytes=CAP)


def test_a_normal_produced_file_still_lands(layout: tuple[Path, Path, Path]) -> None:
    host_out, _, workspace = layout
    LocalDockerSandbox._copy_produced_sync(  # noqa: SLF001
        host_out, workspace / "uploads" / "chart.png", "s-1", "chart.png", workspace
    )
    assert (workspace / "uploads" / "chart.png").read_bytes() == b"\x89PNG"


def test_unsafe_names_missing_files_and_the_size_cap(layout: tuple[Path, Path, Path]) -> None:
    host_out, _, _ = layout
    for ref in ("../srv/.env", "/etc/passwd", "a" + chr(92) + "b"):
        with pytest.raises(ProducedFileRefusedError):
            read_produced_file(host_out, ref, session_id="s-1", cap_bytes=CAP)
    with pytest.raises(CodeSandboxError) as missing:
        read_produced_file(host_out, "nope.png", session_id="s-1", cap_bytes=CAP)
    assert missing.value.context["reason"] == "produced_file_missing"
    with pytest.raises(ProducedFileSizeError):
        read_produced_file(host_out, "chart.png", session_id="s-1", cap_bytes=2)


def test_the_listing_never_enters_a_junction(layout: tuple[Path, Path, Path]) -> None:
    """A junction reads as a folder through its own attributes; only the reparse check
    keeps the walk out of it (Windows, where junctions exist)."""
    if sys.platform != "win32":
        pytest.skip("directory junctions exist only on Windows")
    import _winapi

    host_out, secret_dir, _ = layout
    _winapi.CreateJunction(str(secret_dir), str(host_out / "junc"))
    assert [name for name, _ in list_produced_files(host_out)] == ["chart.png"]
