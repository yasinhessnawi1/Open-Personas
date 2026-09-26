"""The Windows lexical path rules (Spec WIN, T1.3), tested on every platform.

The rules are pure string checks, so they are pinned here everywhere; the resolver
applies them only on Windows (see ``test_sandbox_windows_resolver.py``).
"""

from __future__ import annotations

import pytest
from persona.tools._winpath import windows_path_violation

BS = chr(92)


@pytest.mark.parametrize(
    ("requested", "reason"),
    [
        # backslash anywhere, including every UNC, device and drive-rooted form
        (f"a{BS}b.txt", "mixed_separators"),
        (f"notes{BS}..{BS}..{BS}secret.txt", "mixed_separators"),
        (f"{BS}{BS}server{BS}share{BS}x.txt", "mixed_separators"),
        (f"{BS}{BS}?{BS}C:{BS}Windows", "mixed_separators"),
        (f"{BS}{BS}.{BS}NUL", "mixed_separators"),
        (f"{BS}??{BS}C:{BS}x", "mixed_separators"),
        (f"C:{BS}Windows{BS}win.ini", "mixed_separators"),
        (f"{BS}Windows{BS}win.ini", "mixed_separators"),
        # drive-relative and drive paths with forward slashes
        ("C:x.txt", "absolute"),
        ("c:/Windows/win.ini", "absolute"),
        # alternate data streams and characters Windows forbids
        ("ok.txt:stream", "reserved_character"),
        ("ok.txt::$DATA", "reserved_character"),
        ("dir/a:b", "reserved_character"),
        ("what?.txt", "reserved_character"),
        ("a*.txt", "reserved_character"),
        ('quote".txt', "reserved_character"),
        ("pipe|.txt", "reserved_character"),
        ("less<.txt", "reserved_character"),
        ("tab\there.txt", "reserved_character"),
        # reserved device names, with or without an extension, any case
        ("NUL", "reserved_name"),
        ("nul.txt", "reserved_name"),
        ("out/con.md", "reserved_name"),
        ("PRN", "reserved_name"),
        ("aux.tar.gz", "reserved_name"),
        ("COM1.log", "reserved_name"),
        ("lpt9", "reserved_name"),
        ("COM\u00b9", "reserved_name"),
        ("CONIN$", "reserved_name"),
        ("NUL .txt", "reserved_name"),
        # a trailing dot or space is dropped by Windows, so it would open another file
        ("a.txt.", "trailing_dot_or_space"),
        ("a.txt ", "trailing_dot_or_space"),
        ("folder./a.txt", "trailing_dot_or_space"),
        ("...", "trailing_dot_or_space"),
    ],
)
def test_windows_only_path_shapes_are_refused_with_their_reason(
    requested: str, reason: str
) -> None:
    violation = windows_path_violation(requested)
    assert violation is not None, requested
    assert violation[1] == reason


@pytest.mark.parametrize(
    "requested",
    [
        "report.md",
        "out/report.md",
        "a.b.c.txt",
        ".hidden",
        "CONSOLE.txt",
        "console",
        "nullable.txt",
        "COM10",
        "LPTX.md",
        "sub/../ok.txt",
        "./ok.txt",
        "x" * 300,
        "",
        "caf\u00e9/\u6587\u4ef6.txt",
    ],
)
def test_ordinary_names_pass(requested: str) -> None:
    assert windows_path_violation(requested) is None
