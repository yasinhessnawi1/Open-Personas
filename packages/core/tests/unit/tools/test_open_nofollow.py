"""Adversarial TOCTOU tests for the shared ``open_nofollow`` opener (Spec R2, T3 / F-03).

``resolve_sandbox_path`` blocks *logical* escape, but there is a narrow window
between the resolver's symlink check and the actual ``open()`` where the final
path component can be swapped for a symlink pointing outside the sandbox root
(a TOCTOU race). ``open_nofollow`` closes that window with ``O_NOFOLLOW`` (on
every platform — R2-D-5; ``openat2(RESOLVE_NO_SYMLINKS)`` is an unbuilt Linux-only
future, R2-R-1, which would additionally cover the intermediate-dir case).

These tests demonstrate the exploit on the *final* component: a plain open would
follow the swapped symlink and read/clobber an out-of-sandbox target; the
``O_NOFOLLOW`` opener rejects it with an ``OSError`` (ELOOP).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from persona.tools._sandbox import (
    is_regular_file_nofollow,
    open_nofollow,
    read_nofollow_bytes,
    write_nofollow_bytes,
)


def test_reads_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "ok.txt"
    target.write_bytes(b"hello")
    assert read_nofollow_bytes(target, root=tmp_path) == b"hello"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Windows reports only a read-only bit in st_mode, so 0o600 cannot be read back; "
        "the owner-only property is proven on Windows by test_winopen.py::"
        "test_a_created_file_is_readable_only_by_its_user_and_the_system"
    ),
)
def test_writes_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "out.bin"
    write_nofollow_bytes(target, b"data")
    assert target.read_bytes() == b"data"
    # 0o600 mode for a freshly created file (single-user CLI posture).
    assert (target.stat().st_mode & 0o777) == 0o600


def test_read_rejects_a_symlink_swapped_in_after_resolve(tmp_path: Path) -> None:
    """The headline TOCTOU case: the resolver validated a plain path, then the
    final component became a symlink to an out-of-sandbox secret before the open."""
    outside = tmp_path / "outside_secret.txt"
    outside.write_bytes(b"TOP SECRET - outside the sandbox")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    victim = sandbox / "artifact.png"  # the path the resolver blessed
    # The swap: the final component is now a symlink pointing OUTSIDE.
    victim.symlink_to(outside)

    # A plain open would follow the link and leak the outside secret …
    assert Path(victim).read_bytes() == b"TOP SECRET - outside the sandbox"
    # … but the O_NOFOLLOW opener refuses (ELOOP on the trailing symlink).
    with pytest.raises(OSError):  # noqa: PT011 — ELOOP is an OSError subclass family
        read_nofollow_bytes(victim, root=sandbox)


def test_write_rejects_a_symlink_swapped_in_after_resolve(tmp_path: Path) -> None:
    """A write through a swapped symlink would clobber an out-of-sandbox file."""
    outside = tmp_path / "outside_target.txt"
    outside.write_bytes(b"original")

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    victim = sandbox / "mirror.txt"
    victim.symlink_to(outside)

    with pytest.raises(OSError):  # noqa: PT011
        write_nofollow_bytes(victim, b"CLOBBERED", root=sandbox)
    # The out-of-sandbox target is untouched.
    assert outside.read_bytes() == b"original"


def test_open_nofollow_returns_an_fd_for_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "fd.txt"
    target.write_bytes(b"x")
    fd = open_nofollow(target, os.O_RDONLY, root=tmp_path)
    try:
        assert os.read(fd, 1) == b"x"
    finally:
        os.close(fd)


def test_is_regular_file_nofollow_true_for_a_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "real.txt"
    target.write_bytes(b"x")
    assert is_regular_file_nofollow(target, root=tmp_path) is True


def test_is_regular_file_nofollow_false_for_a_symlink_to_a_regular_file(tmp_path: Path) -> None:
    """The confused-deputy guard for the delete/serve sites: a symlink to a real
    out-of-sandbox file reads as NOT a regular file (``is_file()`` would follow it)."""
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"data")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    assert link.is_file() is True  # the stdlib check FOLLOWS the symlink …
    assert is_regular_file_nofollow(link, root=tmp_path) is False  # … the no-follow check does not.


def test_is_regular_file_nofollow_false_for_missing_path(tmp_path: Path) -> None:
    assert is_regular_file_nofollow(tmp_path / "nope.txt", root=tmp_path) is False


def test_is_regular_file_nofollow_false_for_a_directory(tmp_path: Path) -> None:
    assert is_regular_file_nofollow(tmp_path, root=tmp_path.parent) is False
