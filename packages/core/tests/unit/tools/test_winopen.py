"""The Windows no-follow primitives (Spec WIN, T1): real links, real races, real ACLs.

These tests run only on Windows, because :mod:`persona.tools._winopen` exists only
there; the POSIX opener keeps its own tests in ``test_open_nofollow.py`` unchanged.

Every link here is real: symbolic links (``os.symlink``), directory junctions
(``_winapi.CreateJunction``, no privilege needed) and hard links (``os.link``).
Creating a symbolic link needs Developer Mode or an elevated process; without it the
symlink cases skip locally but FAIL in CI, so the guarantee is never silently unproven.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import sys
import threading
from typing import TYPE_CHECKING

import pytest

if sys.platform != "win32":
    pytest.skip("the Windows no-follow primitives exist only on Windows", allow_module_level=True)

import _winapi
from ctypes import wintypes

from persona.errors import WorkspaceLinkRefusedError
from persona.tools import _winopen

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

BACKSLASH = chr(92)
SECRET = b"OUTSIDE SECRET"
INSIDE = b"INSIDE"


# --- fixtures ----------------------------------------------------------------------------
@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """``(root, outside)``: a workspace root and a sibling folder holding a secret."""
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    root.mkdir()
    (outside / "secret.txt").write_bytes(SECRET)
    (outside / "dir" / "f.txt").write_bytes(SECRET)
    return root, outside


def _symlink(target: str | Path, link: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to create symbolic links to prove the guarantee: {exc}")
        pytest.skip(f"creating a symbolic link needs Developer Mode or elevation here: {exc}")


def _junction(target: Path, link: Path) -> None:
    _winapi.CreateJunction(str(target), str(link))


def _outside_untouched(outside: Path) -> None:
    assert (outside / "secret.txt").read_bytes() == SECRET
    assert (outside / "dir" / "f.txt").read_bytes() == SECRET
    assert sorted(p.name for p in outside.iterdir()) == ["dir", "secret.txt"]
    assert sorted(p.name for p in (outside / "dir").iterdir()) == ["f.txt"]


def _read(root: Path, path: Path) -> bytes:
    fd = _winopen.open_fd(root, path, os.O_RDONLY)
    try:
        chunks = []
        while chunk := os.read(fd, 1 << 16):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write(root: Path, path: Path, data: bytes, flags: int | None = None) -> None:
    fd = _winopen.open_fd(root, path, flags or (os.O_WRONLY | os.O_CREAT | os.O_TRUNC))
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _assert_link_refused(exc: BaseException, reason: str = "link_refused") -> None:
    assert isinstance(exc, WorkspaceLinkRefusedError)
    assert isinstance(exc, OSError)
    assert exc.errno == errno.ELOOP
    assert exc.context["reason"] == reason
    assert "NTSTATUS" not in str(exc)
    assert "0xC" not in str(exc)


# --- plain files ------------------------------------------------------------------------
def test_reads_and_writes_plain_files_below_the_root(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    (root / "sub").mkdir()
    _write(root, root / "sub" / "a.txt", b"hello")
    assert _read(root, root / "sub" / "a.txt") == b"hello"


def test_bytes_round_trip_exactly_so_the_descriptor_is_binary(layout: tuple[Path, Path]) -> None:
    """Text mode would turn LF into CRLF on write and stop reading at 0x1A."""
    root, _ = layout
    payload = bytes(range(256)) + b"a\nb\r\nc\x1ad"
    _write(root, root / "bin.dat", payload)
    assert (root / "bin.dat").read_bytes() == payload
    assert _read(root, root / "bin.dat") == payload


def test_the_descriptor_is_not_inherited_by_child_processes(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    (root / "a.txt").write_bytes(b"x")
    fd = _winopen.open_fd(root, root / "a.txt", os.O_RDONLY)
    try:
        assert os.get_inheritable(fd) is False
    finally:
        os.close(fd)


def test_truncate_empties_an_existing_file(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    (root / "a.txt").write_bytes(b"a much longer original")
    _write(root, root / "a.txt", b"new")
    assert (root / "a.txt").read_bytes() == b"new"


def test_exclusive_create_refuses_an_existing_file(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    (root / "a.txt").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        _write(root, root / "a.txt", b"y", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    assert (root / "a.txt").read_bytes() == b"x"


def test_missing_file_and_directory_map_to_the_posix_errors(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    (root / "d").mkdir()
    with pytest.raises(FileNotFoundError):
        _read(root, root / "missing.txt")
    with pytest.raises(FileNotFoundError):
        _read(root, root / "no" / "such.txt")
    with pytest.raises(IsADirectoryError):
        _read(root, root / "d")


def test_a_read_only_descriptor_cannot_write(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    (root / "a.txt").write_bytes(b"x")
    fd = _winopen.open_fd(root, root / "a.txt", os.O_RDONLY)
    try:
        with pytest.raises(OSError):  # noqa: PT011 - EBADF, the exact errno is the C runtime's
            os.write(fd, b"y")
    finally:
        os.close(fd)


@pytest.mark.parametrize("flag", ["O_APPEND", "O_TEXT", "O_TEMPORARY", "O_SHORT_LIVED"])
def test_flags_that_would_change_meaning_are_refused_loudly(
    layout: tuple[Path, Path], flag: str
) -> None:
    root, _ = layout
    with pytest.raises(ValueError, match="not supported"):
        _winopen.open_fd(root, root / "a.txt", os.O_WRONLY | os.O_CREAT | getattr(os, flag))
    assert not (root / "a.txt").exists()


def test_a_path_outside_the_root_is_refused_before_any_open(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    with pytest.raises(ValueError):  # noqa: PT011 - relative_to's own message
        _winopen.open_fd(root, outside / "secret.txt", os.O_RDONLY)
    with pytest.raises(ValueError, match="unsafe path component"):
        _winopen.open_fd(root, root / "a.txt:stream", os.O_WRONLY | os.O_CREAT)
    _outside_untouched(outside)


# --- links -------------------------------------------------------------------------------
def test_a_final_symlink_is_refused_for_read_and_write(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    _symlink(outside / "secret.txt", root / "link.txt")
    assert (root / "link.txt").read_bytes() == SECRET  # a plain open follows it
    for op in (
        lambda: _read(root, root / "link.txt"),
        lambda: _write(root, root / "link.txt", b"X"),
    ):
        with pytest.raises(OSError) as info:  # noqa: PT011 - asserted precisely below
            op()
        _assert_link_refused(info.value)
    _outside_untouched(outside)


def test_a_dangling_symlink_is_refused_and_creates_nothing_outside(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    _symlink(outside / "created.txt", root / "dangling.txt")
    with pytest.raises(OSError) as info:  # noqa: PT011
        _write(root, root / "dangling.txt", b"X")
    _assert_link_refused(info.value)
    _outside_untouched(outside)


def test_a_junction_in_the_middle_of_the_path_is_refused(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    _junction(outside / "dir", root / "junc")
    assert (root / "junc" / "f.txt").read_bytes() == SECRET  # a plain open follows it
    for op in (
        lambda: _read(root, root / "junc" / "f.txt"),
        lambda: _write(root, root / "junc" / "f.txt", b"X"),
        lambda: _write(root, root / "junc" / "new.txt", b"X"),
    ):
        with pytest.raises(OSError) as info:  # noqa: PT011
            op()
        _assert_link_refused(info.value)
    _outside_untouched(outside)


def test_a_directory_symlink_in_the_middle_of_the_path_is_refused(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    _symlink(outside / "dir", root / "dirlink", directory=True)
    with pytest.raises(OSError) as info:  # noqa: PT011
        _write(root, root / "dirlink" / "f.txt", b"X")
    _assert_link_refused(info.value)
    _outside_untouched(outside)


def test_a_junction_as_the_final_component_is_refused(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    _junction(outside / "dir", root / "junc")
    with pytest.raises(OSError):  # noqa: PT011 - a directory reparse point: refused either way
        _write(root, root / "junc", b"X")
    _outside_untouched(outside)


def test_a_symlink_to_a_network_share_is_refused_without_reaching_the_network(
    layout: tuple[Path, Path],
) -> None:
    """Following it would make Windows contact the host (WinError 67 or 1231 here, and an
    NTLM authentication against a real host). A link refusal proves it was never followed."""
    root, outside = layout
    unc = BACKSLASH * 2 + "127.0.0.1" + BACKSLASH + "nosuchshare$" + BACKSLASH + "x.txt"
    _symlink(unc, root / "unc.txt")
    for op in (lambda: _read(root, root / "unc.txt"), lambda: _write(root, root / "unc.txt", b"X")):
        with pytest.raises(OSError) as info:  # noqa: PT011
            op()
        _assert_link_refused(info.value)


def test_a_hard_link_to_an_outside_file_is_refused_for_read_and_write(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    os.link(outside / "secret.txt", root / "hard.txt")
    for op in (
        lambda: _read(root, root / "hard.txt"),
        lambda: _write(root, root / "hard.txt", b"X"),
    ):
        with pytest.raises(OSError) as info:  # noqa: PT011
            op()
        _assert_link_refused(info.value, "hard_link_refused")
    _outside_untouched(outside)


def test_the_handle_check_refuses_a_reparse_point_even_if_the_open_let_one_through(
    layout: tuple[Path, Path],
) -> None:
    """Belt and braces: ``OBJ_DONT_REPARSE`` refuses first, so this check is reached only if
    a future build ever lets one through. Drive it directly with a handle on the link itself."""
    root, outside = layout
    _symlink(outside / "secret.txt", root / "link.txt")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.restype = wintypes.HANDLE
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    open_reparse_point, read_attributes, share_all, open_existing = 0x00200000, 0x80, 0x7, 3
    handle = create(
        str(root / "link.txt"),
        read_attributes,
        share_all,
        None,
        open_existing,
        open_reparse_point,
        None,
    )
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        with pytest.raises(OSError) as info:  # noqa: PT011
            _winopen._check_regular_single_name(int(handle), root / "link.txt")
        _assert_link_refused(info.value)
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


# --- the owner-only DACL (the Windows reading of 0o600) ---------------------------------
def _dacl_sddl(path: Path) -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi32.GetFileSecurityW
    get_security.restype = wintypes.BOOL
    get_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    to_string = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
    to_string.restype = wintypes.BOOL
    to_string.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.ULONG),
    ]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    dacl_info = 4
    needed = wintypes.DWORD(0)
    get_security(str(path), dacl_info, None, 0, ctypes.byref(needed))
    buffer = ctypes.create_string_buffer(needed.value)
    assert get_security(str(path), dacl_info, buffer, needed, ctypes.byref(needed))
    text = wintypes.LPWSTR()
    assert to_string(buffer, 1, dacl_info, ctypes.byref(text), None)
    try:
        return str(text.value)
    finally:
        kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))


def test_a_created_file_is_readable_only_by_its_user_and_the_system(
    layout: tuple[Path, Path],
) -> None:
    """The Windows twin of the POSIX ``0o600`` assertion: a protected DACL with exactly two
    full-control entries, the current user and SYSTEM, and nothing inherited."""
    root, _ = layout
    _write(root, root / "new.txt", b"x")
    sddl = _dacl_sddl(root / "new.txt")
    assert sddl.startswith("D:P"), sddl
    aces = sddl[3:].strip("()").split(")(")
    trustees = sorted(ace.rsplit(";", 1)[1] for ace in aces)
    assert len(aces) == 2, sddl
    assert all(ace.startswith("A;;FA;;;") for ace in aces), sddl
    assert "SY" in trustees, sddl
    assert [t for t in trustees if t != "SY"][0].startswith("S-1-5-"), sddl
    assert "ID" not in sddl  # nothing inherited from the folder


# --- folders, probe and delete ----------------------------------------------------------
def test_make_dirs_creates_nested_folders_and_the_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "fresh"
    _winopen.make_dirs(root, root / "a" / "b" / "c")
    assert (root / "a" / "b" / "c").is_dir()
    _winopen.make_dirs(root, root / "a" / "b" / "c")  # idempotent
    _winopen.make_dirs(root, root)  # the root itself is a no-op


def test_make_dirs_through_a_junction_is_refused_and_creates_nothing_outside(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    _junction(outside / "dir", root / "junc")
    with pytest.raises(OSError) as info:  # noqa: PT011
        _winopen.make_dirs(root, root / "junc" / "made")
    _assert_link_refused(info.value)
    _outside_untouched(outside)


def test_is_regular_file_is_true_only_for_a_plain_single_name_file(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    (root / "plain.txt").write_bytes(b"x")
    (root / "d").mkdir()
    _junction(outside / "dir", root / "junc")
    os.link(outside / "secret.txt", root / "hard.txt")
    assert _winopen.is_regular_file(root, root / "plain.txt") is True
    for name in ("missing.txt", "d", "junc", "junc/f.txt", "hard.txt"):
        assert _winopen.is_regular_file(root, root / name) is False, name


def test_delete_removes_a_plain_file_and_reports_whether_it_existed(
    layout: tuple[Path, Path],
) -> None:
    root, _ = layout
    (root / "a.txt").write_bytes(b"x")
    assert _winopen.delete_regular_file(root, root / "a.txt") is True
    assert not (root / "a.txt").exists()
    assert _winopen.delete_regular_file(root, root / "a.txt") is False


def test_delete_through_a_junction_or_of_a_hard_link_leaves_the_outside_file(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    _junction(outside / "dir", root / "junc")
    os.link(outside / "secret.txt", root / "hard.txt")
    assert _winopen.delete_regular_file(root, root / "junc" / "f.txt") is False
    assert _winopen.delete_regular_file(root, root / "hard.txt") is False
    _outside_untouched(outside)


def test_delete_of_a_symlink_leaves_both_the_link_and_its_target(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    _symlink(outside / "secret.txt", root / "link.txt")
    assert _winopen.delete_regular_file(root, root / "link.txt") is False
    assert (root / "link.txt").is_symlink()
    _outside_untouched(outside)


# --- handle hygiene ----------------------------------------------------------------------
def _handle_count() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    count = wintypes.DWORD()
    assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
    return int(count.value)


def test_no_handle_leaks_on_success_or_refusal(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    (root / "a.txt").write_bytes(b"x")
    _junction(outside / "dir", root / "junc")
    os.link(outside / "secret.txt", root / "hard.txt")
    operations: list[Callable[[], object]] = [
        lambda: _read(root, root / "a.txt"),
        lambda: _write(root, root / "b.txt", b"y"),
        lambda: _winopen.is_regular_file(root, root / "a.txt"),
        lambda: _winopen.make_dirs(root, root / "d" / "e"),
    ]
    refusals: list[Callable[[], object]] = [
        lambda: _read(root, root / "junc" / "f.txt"),
        lambda: _read(root, root / "hard.txt"),
        lambda: _read(root, root / "missing.txt"),
        lambda: _winopen.make_dirs(root, root / "junc" / "x"),
    ]
    for op in operations + refusals:  # warm up any one-time allocations
        with contextlib.suppress(OSError):
            op()
    before = _handle_count()
    for _ in range(50):
        for op in operations + refusals:
            with contextlib.suppress(OSError):
                op()
    assert _handle_count() - before < 10  # 400 operations; a leak would add hundreds


# --- a component swapped mid-operation --------------------------------------------------
def _race(
    root: Path, outside: Path, target: Path, swap_once: Callable[[], None], iterations: int
) -> tuple[int, int, int]:
    """Run ``iterations`` alternating reads and writes of ``target`` while another thread
    keeps swapping a component. Returns ``(escapes, successes, swaps)``, where an escape is
    read outside content or any change to the outside folder (the durable record)."""
    stop = threading.Event()
    swaps = [0]

    def swapper() -> None:
        while not stop.is_set():
            try:
                swap_once()
                swaps[0] += 1
            except OSError:
                pass

    thread = threading.Thread(target=swapper, daemon=True)
    thread.start()
    escapes = successes = 0
    try:
        for i in range(iterations):
            try:
                if i % 2:
                    _write(root, target, INSIDE)
                elif _read(root, target) == SECRET:
                    escapes += 1
                successes += 1
            except OSError:
                pass
            if (outside / "secret.txt").read_bytes() != SECRET or (
                outside / "dir" / "f.txt"
            ).read_bytes() != SECRET:
                escapes += 1
                (outside / "secret.txt").write_bytes(SECRET)
                (outside / "dir" / "f.txt").write_bytes(SECRET)
    finally:
        stop.set()
        thread.join()
    return escapes, successes, swaps[0]


def test_a_final_component_swapped_to_a_symlink_mid_operation_never_escapes(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    target = root / "race.txt"
    target.write_bytes(INSIDE)
    _symlink(outside / "secret.txt", root / "_probe")
    (root / "_probe").unlink()

    def swap_once() -> None:
        link, plain = root / "_link", root / "_plain"
        for leftover in (link, plain):  # a swap that lost a sharing race leaves one behind
            if leftover.is_symlink() or leftover.exists():
                leftover.unlink()
        link.symlink_to(outside / "secret.txt")
        link.replace(target)
        plain.write_bytes(INSIDE)
        plain.replace(target)

    escapes, successes, swaps = _race(root, outside, target, swap_once, 1500)
    assert swaps > 50, "the swapper must really have raced the opener"
    assert successes > 0, "the plain state must have been opened at least once"
    assert escapes == 0


def test_an_intermediate_folder_swapped_to_a_junction_mid_operation_never_escapes(
    layout: tuple[Path, Path],
) -> None:
    root, outside = layout
    real, parked = root / "rdir", root / "_parked"
    real.mkdir()
    (real / "f.txt").write_bytes(INSIDE)

    def swap_once() -> None:
        real.rename(parked)
        try:
            _junction(outside / "dir", real)
            real.rmdir()  # removes the junction, never its target
        finally:
            parked.rename(real)

    escapes, successes, swaps = _race(root, outside, real / "f.txt", swap_once, 1500)
    assert swaps > 50, "the swapper must really have raced the opener"
    assert successes > 0, "the plain state must have been opened at least once"
    assert escapes == 0


# --- defence-in-depth name checks (T1.5 INFO) ------------------------------------------------
@pytest.mark.parametrize("name", ["NUL", "con.txt", "COM1.log", "a.txt.", "a.txt "])
def test_the_opener_refuses_names_windows_would_alias(layout: tuple[Path, Path], name: str) -> None:
    """The resolver refuses these first; the opener refuses them again, because NT would
    create exactly that name, which Win32 later maps to a device or to another file."""
    root, _ = layout
    with pytest.raises(ValueError, match="unsafe path component"):
        _winopen.open_fd(root, root / name, os.O_WRONLY | os.O_CREAT)
    assert list(root.iterdir()) == []


def test_a_name_too_long_for_the_kernel_string_is_refused_before_any_call(
    layout: tuple[Path, Path],
) -> None:
    """A UNICODE_STRING length is 16 bits; a longer name must never be truncated silently."""
    root, _ = layout
    deep = root.joinpath(*(["abcdefghij"] * 3500))  # about 38500 characters
    with pytest.raises(OSError) as info:  # noqa: PT011 - the errno is asserted below
        _winopen.open_fd(root, deep, os.O_RDONLY)
    assert info.value.errno == errno.ENAMETOOLONG
