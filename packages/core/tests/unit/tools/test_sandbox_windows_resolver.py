"""The sandbox resolver on Windows (Spec WIN, T1.3).

On Windows the resolver refuses Windows-only path shapes before ANY filesystem call
(resolving a UNC or device path has side effects there, such as authenticating to a
remote host), and its escape check is lexical: it never follows a link, and the
no-follow opener refuses every link below the root when the path is opened.
"""

from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

if sys.platform != "win32":
    pytest.skip("the resolver's Windows branch runs only on Windows", allow_module_level=True)

import nt

from persona.errors import SandboxViolationError, WorkspaceLinkRefusedError
from persona.tools._sandbox import read_nofollow_bytes, resolve_sandbox_path

if TYPE_CHECKING:
    from collections.abc import Callable

BS = chr(92)

#: Inputs that must be refused, with the reason, BEFORE any filesystem call.
REFUSED = [
    (f"notes{BS}evil.txt", "mixed_separators"),  # a backslash inside one component
    (f"a{BS}..{BS}..{BS}secret.txt", "mixed_separators"),
    (f"{BS}{BS}attacker.example{BS}share{BS}x.txt", "mixed_separators"),  # UNC
    (f"{BS}{BS}?{BS}C:{BS}Windows{BS}win.ini", "mixed_separators"),
    (f"{BS}{BS}.{BS}PhysicalDrive0", "mixed_separators"),
    (f"{BS}??{BS}C:{BS}Windows", "mixed_separators"),
    (f"C:{BS}Windows{BS}win.ini", "mixed_separators"),
    (f"{BS}Windows{BS}win.ini", "mixed_separators"),
    ("//attacker.example/share/x.txt", "absolute"),  # UNC with forward slashes
    ("/Windows/win.ini", "absolute"),
    ("C:x.txt", "absolute"),
    ("C:/Windows/win.ini", "absolute"),
    ("ok.txt:stream", "reserved_character"),
    ("ok.txt::$DATA", "reserved_character"),
    ("what?.txt", "reserved_character"),
    ("NUL", "reserved_name"),
    ("con.txt", "reserved_name"),
    ("out/COM1.log", "reserved_name"),
    ("a.txt.", "trailing_dot_or_space"),
    ("a.txt ", "trailing_dot_or_space"),
]

_TECHNICAL_NOISE = ("POSIX", "Errno", "WinError", "NTSTATUS", "Traceback")


class FilesystemTouchedError(AssertionError):
    """Raised by the patched entry points: the resolver touched the filesystem."""


_FILESYSTEM_ENTRY_POINTS: list[tuple[object, str]] = [
    (Path, "resolve"),
    (Path, "stat"),
    (Path, "lstat"),
    (Path, "exists"),
    (Path, "is_dir"),
    (Path, "is_file"),
    (Path, "is_symlink"),
    (Path, "readlink"),
    (os, "stat"),
    (os, "lstat"),
    (os, "open"),
    (os, "readlink"),
    (os, "scandir"),
    (os, "listdir"),
    (os.path, "realpath"),
    (os.path, "exists"),
    (nt, "_getfinalpathname"),
    (builtins, "open"),
]


def _resolve_with_no_filesystem(
    monkeypatch: pytest.MonkeyPatch, root: Path, requested: str
) -> BaseException | None:
    """Run the resolver with every filesystem entry point it could reach made to raise.

    The patches are active ONLY around the resolver call (pytest itself needs the real
    ones), and the outcome is returned so it is asserted after they are removed.
    """

    def touched(*_args: object, **_kwargs: object) -> NoReturn:
        raise FilesystemTouchedError("the resolver touched the filesystem")

    with monkeypatch.context() as patched:
        for owner, name in _FILESYSTEM_ENTRY_POINTS:
            patched.setattr(owner, name, touched)
        try:
            resolve_sandbox_path(root, requested)
        except BaseException as exc:  # noqa: BLE001 - returned and asserted by the caller
            return exc
    return None


@pytest.mark.parametrize(("requested", "reason"), REFUSED)
def test_windows_shapes_are_refused_before_any_filesystem_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested: str, reason: str
) -> None:
    outcome = _resolve_with_no_filesystem(monkeypatch, tmp_path, requested)
    assert isinstance(outcome, SandboxViolationError), repr(outcome)
    assert outcome.context["reason"] == reason


def test_the_filesystem_guard_is_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control, so the test above cannot pass while seeing nothing: an
    ordinary path does reach the filesystem (the trusted root is resolved)."""
    outcome = _resolve_with_no_filesystem(monkeypatch, tmp_path, "report.md")
    assert isinstance(outcome, FilesystemTouchedError), repr(outcome)


@pytest.mark.parametrize(("requested", "reason"), REFUSED)
def test_every_refusal_reads_human_and_says_how_to_fix_it(
    tmp_path: Path, requested: str, reason: str
) -> None:
    with pytest.raises(SandboxViolationError) as info:
        resolve_sandbox_path(tmp_path, requested)
    message = str(info.value)
    assert f"[reason={reason}]" in message
    assert "'out/report.md'" in message  # every hint names a concrete valid form
    for noise in _TECHNICAL_NOISE:
        assert noise not in message


def test_a_backslash_inside_one_component_gets_the_forward_slash_hint(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolationError) as info:
        resolve_sandbox_path(tmp_path, f"notes{BS}evil.txt")
    message = str(info.value)
    assert message.startswith("backslash in path [reason=mixed_separators]; ")
    assert "Use forward slashes '/'" in message


def test_ordinary_paths_resolve_lexically_below_the_root(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    assert resolve_sandbox_path(tmp_path, "out/report.md") == root / "out" / "report.md"
    assert resolve_sandbox_path(tmp_path, "sub/../ok.txt") == root / "ok.txt"
    with pytest.raises(SandboxViolationError) as info:
        resolve_sandbox_path(tmp_path, "a/../../outside.txt")
    assert info.value.context["reason"] == "escape"


def test_a_junction_out_is_an_escape_and_a_junction_in_is_refused_as_a_link(
    tmp_path: Path,
) -> None:
    """A link leading out of the root is an escape, exactly as resolve() made it on
    POSIX. A link that stays inside is refused too (ruling 3), with a human message:
    no path the resolver returns went through a link."""
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (root / "real").mkdir(parents=True)
    (outside / "dir" / "f.txt").write_bytes(b"OUTSIDE")
    (root / "real" / "f.txt").write_bytes(b"INSIDE")
    import _winapi

    _winapi.CreateJunction(str(outside / "dir"), str(root / "out_junc"))
    _winapi.CreateJunction(str(root / "real"), str(root / "in_junc"))
    with pytest.raises(SandboxViolationError) as info:
        resolve_sandbox_path(root, "out_junc/f.txt")
    assert info.value.context["reason"] == "escape"
    for requested in ("in_junc/f.txt", "in_junc", "in_junc/new.txt", "real/../in_junc/f.txt"):
        with pytest.raises(SandboxViolationError) as info:
            resolve_sandbox_path(root, requested)
        assert info.value.context["reason"] == "link", requested
        assert "does not follow links or shortcuts" in str(info.value)
    assert resolve_sandbox_path(root, "real/f.txt") == root.resolve() / "real" / "f.txt"
    # Defence in depth: the opener refuses the link path on its own as well.
    with pytest.raises(WorkspaceLinkRefusedError):
        read_nofollow_bytes(root.resolve() / "in_junc" / "f.txt", root=root)


def test_a_folder_swapped_for_a_junction_after_the_walk_is_refused_at_open(
    tmp_path: Path,
) -> None:
    """The TOCTOU case for the whole flow, made deterministic: the walk approves a plain
    folder, the folder is then swapped for a junction to outside, and the open that
    follows is refused by OBJ_DONT_REPARSE. The walk is advisory; the open enforces."""
    root, outside = tmp_path / "workspace", tmp_path / "outside"
    (root / "rdir").mkdir(parents=True)
    (root / "rdir" / "f.txt").write_bytes(b"INSIDE")
    outside.mkdir()
    (outside / "f.txt").write_bytes(b"OUTSIDE")
    resolved = resolve_sandbox_path(root, "rdir/f.txt")  # approved: no link yet
    (root / "rdir").rename(root / "parked")
    import _winapi

    _winapi.CreateJunction(str(outside), str(root / "rdir"))
    assert (root / "rdir" / "f.txt").read_bytes() == b"OUTSIDE"  # a plain open would follow
    with pytest.raises(WorkspaceLinkRefusedError):
        read_nofollow_bytes(resolved, root=root)


def test_a_planted_link_to_a_network_share_is_an_escape_and_never_contacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before T1.3 the resolver's resolve() followed this link into the network
    redirector (WinError 1231 here; against a real host Windows would authenticate to
    it). Now the link is read, not followed: no system call is ever asked about the
    host, and the path is refused as an escape."""
    host = "127.0.0.1"
    unc = f"{BS}{BS}{host}{BS}nosuchshare${BS}x.txt"
    try:
        (tmp_path / "unc.txt").symlink_to(unc)
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to create symbolic links: {exc}")
        pytest.skip(f"creating a symbolic link needs Developer Mode or elevation here: {exc}")

    real_final = vars(nt)["_getfinalpathname"]  # private, so absent from the type stubs
    real_open, real_stat, real_lstat = os.open, os.stat, os.lstat

    def guarded(original: Callable[..., object]) -> Callable[..., object]:
        def call(path: object, *args: object, **kwargs: object) -> object:
            if host in str(path):
                raise FilesystemTouchedError(f"asked the system about {path!r}")
            return original(path, *args, **kwargs)

        return call

    monkeypatch.setattr(nt, "_getfinalpathname", guarded(real_final))
    monkeypatch.setattr(os, "open", guarded(real_open))
    monkeypatch.setattr(os, "stat", guarded(real_stat))
    monkeypatch.setattr(os, "lstat", guarded(real_lstat))
    with pytest.raises(SandboxViolationError) as info:
        resolve_sandbox_path(tmp_path, "unc.txt")
    assert info.value.context["reason"] == "escape"
    with pytest.raises(SandboxViolationError):
        resolve_sandbox_path(tmp_path, "unc.txt/../unc.txt")


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        (f"{BS}{BS}host{BS}share{BS}x", None),
        ("//host/share/x", None),
        (f"{BS}{BS}?{BS}UNC{BS}host{BS}share{BS}x", None),
        (f"{BS}{BS}?{BS}Volume{{1234}}{BS}x", None),
        (f"{BS}{BS}.{BS}NUL", None),
        (f"{BS}{BS}?{BS}C:{BS}data{BS}x", f"C:{BS}data{BS}x"),
        (f"D:{BS}data{BS}x", f"D:{BS}data{BS}x"),
        (f"{BS}etc{BS}passwd", f"C:{BS}etc{BS}passwd"),
        (f"rel{BS}y", f"C:{BS}ws{BS}rel{BS}y"),
        (f"..{BS}..{BS}up", f"C:{BS}up"),
        # An NT-namespace target is only a local name here ("??" is not a valid file
        # name), so it can never reach the object manager; the escape check refuses it.
        (f"{BS}??{BS}C:{BS}x", f"C:{BS}??{BS}C:{BS}x"),
    ],
)
def test_a_link_target_is_walked_into_only_when_it_names_a_drive(
    target: str, expected: str | None
) -> None:
    """Every target shape a link can carry: network shares and devices are never local."""
    from pathlib import PureWindowsPath

    from persona.tools._winresolve import _local_target

    result = _local_target(Path(f"C:{BS}ws{BS}link"), target)
    if expected is None:
        assert result is None
    else:
        assert result == PureWindowsPath(expected)


def test_a_link_loop_is_refused_at_its_first_link_without_walking_it(tmp_path: Path) -> None:
    """Two links that point at each other: the walk stops at the first link below the
    root and refuses it (its target stays inside, so reason ``link``); the loop is never
    followed at all."""
    try:
        (tmp_path / "a").symlink_to(tmp_path / "b", target_is_directory=True)
        (tmp_path / "b").symlink_to(tmp_path / "a", target_is_directory=True)
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to create symbolic links: {exc}")
        pytest.skip(f"creating a symbolic link needs Developer Mode or elevation here: {exc}")
    with pytest.raises(SandboxViolationError) as info:
        resolve_sandbox_path(tmp_path, "a/x.txt")
    assert info.value.context["reason"] == "link"


def _host_guard(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    """Fail if any Win32 path API is asked about ``host`` (a network share or a drive)."""
    for owner, name in [(os, "open"), (os, "stat"), (os, "lstat"), (os, "readlink")]:
        original = getattr(owner, name)

        def call(
            path: object, *args: object, _o: Callable[..., object] = original, **kw: object
        ) -> object:
            if host.lower() in str(path).lower():
                raise FilesystemTouchedError(f"asked the system about {path!r}")
            return _o(path, *args, **kw)

        monkeypatch.setattr(owner, name, call)
    real_final = vars(nt)["_getfinalpathname"]

    def final(path: object) -> object:
        if host.lower() in str(path).lower():
            raise FilesystemTouchedError(f"asked the system about {path!r}")
        return real_final(path)

    monkeypatch.setattr(nt, "_getfinalpathname", final)


def _no_path_probe_below(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Fail if a Win32 path API is asked about any path strictly below ``root``: the walk
    must go by handle only, so a swapped component is never parsed (and followed) by name."""
    prefix = str(root.resolve()).lower() + BS
    for owner, name in [(os, "stat"), (os, "lstat"), (os, "readlink"), (os, "open")]:
        original = getattr(owner, name)

        def call(
            path: object, *args: object, _o: Callable[..., object] = original, **kw: object
        ) -> object:
            if str(path).lower().startswith(prefix):
                raise FilesystemTouchedError(f"a path below the root was parsed: {path!r}")
            return _o(path, *args, **kw)

        monkeypatch.setattr(owner, name, call)


def test_an_intermediate_folder_swapped_for_a_share_link_during_the_walk_is_never_contacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The L-1 race shape, made deterministic: right after the walk opens folder ``d``,
    ``d`` is swapped for a symbolic link to a network share. The walk continues through
    the handle it already holds on the original folder, so nothing is asked about the
    share by name; the path it approved is then refused by the opener."""
    from persona.tools import _winopen

    host = "127.0.0.1"
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_bytes(b"INSIDE")
    try:
        (tmp_path / "probe").symlink_to(tmp_path / "d", target_is_directory=True)
        (tmp_path / "probe").unlink()
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to create symbolic links: {exc}")
        pytest.skip(f"creating a symbolic link needs Developer Mode or elevation here: {exc}")
    real_nt_open = _winopen._nt_open
    swapped: list[bool] = []

    def nt_open_then_swap(root_handle: int, name: str, path: Path, **kwargs: int) -> int:
        handle = real_nt_open(root_handle, name, path, **kwargs)
        if name == "d" and not swapped:
            (tmp_path / "d").rename(tmp_path / "d_parked")
            (tmp_path / "d").symlink_to(f"{BS}{BS}{host}{BS}share", target_is_directory=True)
            swapped.append(True)
        return handle

    monkeypatch.setattr(_winopen, "_nt_open", nt_open_then_swap)
    _host_guard(monkeypatch, host)
    _no_path_probe_below(monkeypatch, tmp_path)
    resolved = resolve_sandbox_path(tmp_path, "d/f.txt")
    assert swapped, "the swap must really have happened during the walk"
    monkeypatch.setattr(_winopen, "_nt_open", real_nt_open)
    with pytest.raises(WorkspaceLinkRefusedError):
        read_nofollow_bytes(resolved, root=tmp_path)


def test_a_link_to_a_mapped_drive_is_an_escape_and_never_probed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        (tmp_path / "share").symlink_to("Z:" + BS + "team", target_is_directory=True)
    except OSError as exc:
        if os.environ.get("CI"):
            pytest.fail(f"CI must be able to create symbolic links: {exc}")
        pytest.skip(f"creating a symbolic link needs Developer Mode or elevation here: {exc}")
    _host_guard(monkeypatch, "Z:")
    with pytest.raises(SandboxViolationError) as info:
        resolve_sandbox_path(tmp_path, "share/report.docx")
    assert info.value.context["reason"] == "escape"


def test_a_workspace_that_does_not_exist_yet_resolves_without_error(tmp_path: Path) -> None:
    """A persona's first file: its workspace folder is created on write, not before."""
    root = tmp_path / "not" / "yet"
    assert resolve_sandbox_path(root, "uploads/chart.png") == (
        root.resolve() / "uploads" / "chart.png"
    )


@pytest.mark.parametrize(
    ("substitute", "expected"),
    [
        (f"{BS}??{BS}C:{BS}data{BS}x", f"C:{BS}data{BS}x"),
        (f"{BS}??{BS}UNC{BS}host{BS}share", f"{BS}{BS}host{BS}share"),
        (f"{BS}??{BS}GLOBALROOT{BS}Device{BS}Mup{BS}host{BS}share", None),
        (f"{BS}??{BS}Volume{{1234}}{BS}x", None),
        (f"{BS}??{BS}PhysicalDrive0", None),
        (f"..{BS}d{BS}f.txt", f"..{BS}d{BS}f.txt"),
    ],
)
def test_a_link_substitute_name_becomes_a_win32_target_only_for_drives_shares_and_relatives(
    substitute: str, expected: str | None
) -> None:
    """T1.6 INFO: an NT-namespace target that is neither a drive nor a share is refused."""
    from persona.tools._winopen import win32_link_target

    assert win32_link_target(substitute) == expected


def test_a_component_whose_kind_cannot_be_read_is_refused_and_its_handle_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1.6 INFO: the walk closes the handle and refuses the path (a sandbox violation,
    never a raw OSError) when reading a component's attributes fails."""
    import ctypes
    from ctypes import wintypes

    from persona.tools import _winopen

    (tmp_path / "d").mkdir()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

    def handles() -> int:
        count = wintypes.DWORD()
        kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
        return int(count.value)

    def failing(_handle: int, path: Path) -> tuple[int, int]:
        raise OSError(5, "access denied", str(path))

    monkeypatch.setattr(_winopen, "_attribute_tag", failing)
    for _ in range(3):  # warm up
        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(tmp_path, "d/f.txt")
    before = handles()
    for _ in range(200):
        with pytest.raises(SandboxViolationError) as info:
            resolve_sandbox_path(tmp_path, "d/f.txt")
    assert info.value.context["reason"] == "link"
    assert handles() - before < 10  # a leak would add one handle per call
