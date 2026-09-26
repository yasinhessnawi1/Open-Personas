"""Adversarial tests for the sandbox path resolver (T09 — WRITTEN FIRST).

Per the spec-03 Phase 1 refinement #8: write all adversarial cases RED
against a stub, then implement ``resolve_sandbox_path`` until they go
GREEN, then run the ``security-reviewer`` subagent on the resulting
``_sandbox.py``.

Adversarial categories (research.md §5.3):
- Parent traversal (.., ../.., etc.)
- Absolute paths (/etc/passwd, Windows-style)
- NULL byte (\\x00)
- Mixed separators on POSIX (\\)
- Long paths (>4096 chars)
- Symlinks escaping the sandbox
- Empty / whitespace-only paths
- Unicode tricks (RTL override, one-dot-leader)

Happy paths:
- Plain file names
- Nested directories
- Symlinks staying inside the sandbox
- Pre-existing & nonexistent targets
"""

# ruff: noqa: ANN401, ARG001, ARG002, ERA001
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest
from persona.errors import SandboxViolationError
from persona.tools._sandbox import resolve_sandbox_path

if TYPE_CHECKING:
    from pathlib import Path

# Section: happy paths


class TestHappyPaths:
    def test_plain_filename(self, tmp_path: Path) -> None:
        result = resolve_sandbox_path(tmp_path, "file.txt")
        assert result.is_relative_to(tmp_path.resolve())
        assert result.name == "file.txt"

    def test_nested_directories(self, tmp_path: Path) -> None:
        result = resolve_sandbox_path(tmp_path, "a/b/c/d.txt")
        assert result.is_relative_to(tmp_path.resolve())
        assert result.name == "d.txt"

    def test_nonexistent_path_resolves(self, tmp_path: Path) -> None:
        # The resolver does NOT require the file to exist (file_write needs to
        # create new files).
        result = resolve_sandbox_path(tmp_path, "does/not/exist/yet.txt")
        assert result.is_relative_to(tmp_path.resolve())

    def test_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "exists.txt"
        target.write_text("content")
        result = resolve_sandbox_path(tmp_path, "exists.txt")
        assert result == target.resolve()

    def test_unicode_filename_allowed(self, tmp_path: Path) -> None:
        # Legitimate Unicode characters in filenames are fine.
        result = resolve_sandbox_path(tmp_path, "norsk-tekst-æøå.txt")
        assert result.is_relative_to(tmp_path.resolve())

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "Spec WIN ruling 3: on Windows every link below the root is refused, even one "
            "that stays inside; proven by test_sandbox_windows_resolver.py::"
            "test_a_junction_out_is_an_escape_and_a_junction_in_is_refused_as_a_link"
        ),
    )
    def test_symlink_inside_sandbox_allowed(self, tmp_path: Path) -> None:
        # A symlink whose target is also inside the sandbox is fine.
        target = tmp_path / "target.txt"
        target.write_text("hello")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = resolve_sandbox_path(tmp_path, "link.txt")
        assert result.is_relative_to(tmp_path.resolve())


# Section: parent traversal


class TestParentTraversal:
    @pytest.mark.parametrize(
        "bad",
        [
            "../etc/passwd",
            "../../etc/passwd",
            "../../../etc/passwd",
            "a/../../etc",
            "a/b/../../../etc",
            "./..",
            "./../",
            "./../../.",
            "../",
            "..",
        ],
    )
    def test_rejects_parent_traversal(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(tmp_path, bad)


# Section: absolute paths


class TestAbsolutePaths:
    @pytest.mark.parametrize(
        "bad",
        [
            "/etc/passwd",
            "/",
            "//etc/passwd",  # double-slash
            "/var/log/syslog",
        ],
    )
    def test_rejects_posix_absolute(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(tmp_path, bad)

    def test_rejects_windows_drive_letter_on_posix(self, tmp_path: Path) -> None:
        # On POSIX, "C:\\Windows" is technically a filename with backslashes —
        # but we reject mixed separators on POSIX anyway (defense in depth).
        if sys.platform != "win32":
            with pytest.raises(SandboxViolationError):
                resolve_sandbox_path(tmp_path, "C:\\Windows\\System32")

    def test_rejects_extended_windows_path_on_posix(self, tmp_path: Path) -> None:
        if sys.platform != "win32":
            with pytest.raises(SandboxViolationError):
                resolve_sandbox_path(tmp_path, "\\\\?\\C:\\Windows")


# Section: NULL byte


class TestNullByte:
    @pytest.mark.parametrize(
        "bad",
        [
            "file.txt\x00.png",
            "a/b\x00/c",
            "\x00",
            "\x00file",
            "file\x00",
        ],
    )
    def test_rejects_null_byte(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(tmp_path, bad)


# Section: mixed separators on POSIX


class TestMixedSeparators:
    def test_rejects_backslash_on_posix(self, tmp_path: Path) -> None:
        # On POSIX, "a\\b\\c" is a single filename containing backslashes —
        # technically inside the sandbox, but operator-confusing. Reject.
        if os.sep == "/":
            with pytest.raises(SandboxViolationError):
                resolve_sandbox_path(tmp_path, "a\\b\\c")

    def test_rejects_windows_style_parent(self, tmp_path: Path) -> None:
        if os.sep == "/":
            with pytest.raises(SandboxViolationError):
                resolve_sandbox_path(tmp_path, "..\\..\\etc")


# Section: long paths (DOS protection)


class TestLongPaths:
    def test_rejects_path_over_cap(self, tmp_path: Path) -> None:
        bad = "a" * 4097
        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(tmp_path, bad)

    def test_accepts_path_just_under_cap(self, tmp_path: Path) -> None:
        # 4096 is the cap; anything <= is allowed.
        ok = "a" * 4096
        result = resolve_sandbox_path(tmp_path, ok)
        assert result.is_relative_to(tmp_path.resolve())


# Section: symlink escape


class TestSymlinkEscape:
    def test_symlink_to_outside_directory_rejected(self, tmp_path: Path) -> None:
        # Create a sandbox subdir; create a symlink inside it pointing to the
        # parent directory (outside the sandbox).
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("escape")
        link = sandbox / "link.txt"
        link.symlink_to(outside)

        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(sandbox, "link.txt")

    def test_symlink_to_etc_rejected(self, tmp_path: Path) -> None:
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        link = sandbox / "etcpasswd"
        link.symlink_to("/etc/passwd")

        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(sandbox, "etcpasswd")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "Spec WIN ruling 3: on Windows every link below the root is refused, even one "
            "that stays inside; proven by test_sandbox_windows_resolver.py::"
            "test_a_junction_out_is_an_escape_and_a_junction_in_is_refused_as_a_link"
        ),
    )
    def test_symlink_chain_inside_allowed(self, tmp_path: Path) -> None:
        # link -> intermediate -> target (all inside sandbox) is fine.
        target = tmp_path / "target.txt"
        target.write_text("ok")
        intermediate = tmp_path / "intermediate"
        intermediate.symlink_to(target)
        link = tmp_path / "link"
        link.symlink_to(intermediate)

        result = resolve_sandbox_path(tmp_path, "link")
        assert result.is_relative_to(tmp_path.resolve())


# Section: empty / whitespace


class TestEmptyOrWhitespace:
    @pytest.mark.parametrize("bad", ["", " ", "\t", "\n", "   ", "\t\n"])
    def test_rejects_empty_or_whitespace(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(tmp_path, bad)


# Section: Unicode visual confusion


class TestUnicodeConfusion:
    def test_rtl_override_does_not_bypass_parent_check(self, tmp_path: Path) -> None:
        # U+202E RIGHT-TO-LEFT OVERRIDE; the bytes after it still resolve as ..
        # The filename contains '..' as substring — Path.resolve doesn't treat
        # the RTL override specially, so the file would actually be created
        # with a weird name INSIDE the sandbox. We allow this — the test
        # documents the behavior (resolves inside, NOT a violation).
        # If the security-reviewer flags this as needing explicit reject,
        # tighten then.
        try:
            result = resolve_sandbox_path(tmp_path, "a/‮/etc")
            assert result.is_relative_to(tmp_path.resolve())
        except SandboxViolationError:
            pass  # Either behavior is acceptable; the test documents both.

    def test_one_dot_leader_not_treated_as_parent(self, tmp_path: Path) -> None:
        # U+2024 ONE DOT LEADER looks like '.' visually but is NOT '.'.
        # Path('․․') is a legitimate filename, not a parent reference.
        result = resolve_sandbox_path(tmp_path, "․․")
        assert result.is_relative_to(tmp_path.resolve())


# Section: error context


class TestErrorContext:
    def test_violation_carries_requested_path_in_context(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxViolationError) as exc_info:
            resolve_sandbox_path(tmp_path, "../etc/passwd")
        # The context should carry enough information for an audit log.
        assert exc_info.value.context  # nonempty
        assert "requested" in exc_info.value.context or "reason" in exc_info.value.context

    def test_violation_message_useful(self, tmp_path: Path) -> None:
        with pytest.raises(SandboxViolationError) as exc_info:
            resolve_sandbox_path(tmp_path, "\x00")
        # The message should mention what was wrong (null byte / sandbox / etc.).
        msg = str(exc_info.value).lower()
        assert "null" in msg or "sandbox" in msg or "byte" in msg

    def test_escape_context_carries_both_requested_and_resolved(self, tmp_path: Path) -> None:
        # Symlink-escape goes through the post-resolve `is_relative_to` branch.
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("escape")
        link = sandbox / "link.txt"
        link.symlink_to(outside)

        with pytest.raises(SandboxViolationError) as exc_info:
            resolve_sandbox_path(sandbox, "link.txt")
        ctx = exc_info.value.context
        assert ctx.get("reason") == "escape"
        assert "requested" in ctx
        assert "resolved" in ctx

    def test_context_strips_null_byte(self, tmp_path: Path) -> None:
        # Per the security review (Finding 1): control characters in user input
        # must NOT survive into the audit-log context as literal NUL bytes.
        with pytest.raises(SandboxViolationError) as exc_info:
            resolve_sandbox_path(tmp_path, "file\x00.png")
        for value in exc_info.value.context.values():
            assert "\x00" not in value


# Section: root-directory reference (single dot — security-review Finding 2)


class TestRootDirectoryReference:
    @pytest.mark.parametrize("bad", [".", " .", ". ", "./"])
    def test_rejects_single_dot(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(tmp_path, bad)


# Section: root that is itself a symlink (macOS /tmp -> /private/tmp)


class TestRootIsSymlink:
    def test_root_symlink_handled_consistently(self, tmp_path: Path) -> None:
        # Make a symlink that points to an actual sandbox dir.
        real_sandbox = tmp_path / "real"
        real_sandbox.mkdir()
        sandbox_via_link = tmp_path / "via_link"
        sandbox_via_link.symlink_to(real_sandbox)

        # A nested file inside the linked sandbox should resolve and pass.
        result = resolve_sandbox_path(sandbox_via_link, "inside.txt")
        # Either canonical (real) path or the link-traversed path is acceptable;
        # what matters is `is_relative_to(root.resolve())` is True.
        assert result.is_relative_to(sandbox_via_link.resolve())

    def test_root_symlink_still_rejects_escape(self, tmp_path: Path) -> None:
        real_sandbox = tmp_path / "real"
        real_sandbox.mkdir()
        sandbox_via_link = tmp_path / "via_link"
        sandbox_via_link.symlink_to(real_sandbox)

        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(sandbox_via_link, "../escape")


# Section: device-file symlinks (defense-in-depth via is_relative_to)


class TestDeviceFileSymlinks:
    def test_symlink_to_dev_null_rejected(self, tmp_path: Path) -> None:
        # OS-general: any symlink whose .resolve() target is outside the
        # sandbox is rejected. /dev/null is the canonical example.
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        link = sandbox / "nullsink"
        try:
            link.symlink_to("/dev/null")
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not supported")

        with pytest.raises(SandboxViolationError):
            resolve_sandbox_path(sandbox, "nullsink")


# Section: side-effect freedom


class TestPureFunction:
    """The resolver must not read/write/access files (it's path manipulation)."""

    def test_does_not_create_path(self, tmp_path: Path) -> None:
        resolve_sandbox_path(tmp_path, "new/dir/file.txt")
        # No directories or files should have been created.
        assert not (tmp_path / "new").exists()

    def test_does_not_require_existing_root(self, tmp_path: Path) -> None:
        # The function should handle a root that exists but is empty without
        # creating anything.
        before = set(tmp_path.iterdir())
        resolve_sandbox_path(tmp_path, "x.txt")
        after = set(tmp_path.iterdir())
        assert before == after
