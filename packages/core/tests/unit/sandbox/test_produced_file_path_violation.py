"""The produced-file discovery rule, pinned rule by rule (Spec WIN T1.5, H-1).

Discovery refuses these names before any host path exists, on every platform. The
resolver downstream refuses most of them again, so each rule is pinned HERE, directly,
with its exact result: a rule dropped from discovery must fail a test on its own.
"""

from __future__ import annotations

import pytest
from persona.sandbox.result import produced_file_path_violation

BS = chr(92)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("", "the name is empty"),
        ("bad\x07name.txt", "the name contains control characters"),
        ("line\nbreak.txt", "the name contains control characters"),
        ("del\x7f.txt", "the name contains control characters"),
        (f"a{BS}b.txt", "the name contains a backslash"),
        (f"C:{BS}Users{BS}x{BS}Startup{BS}x.bat", "the name contains a backslash"),
        ("x.png:hidden", "the name contains a colon"),
        ("C:x.txt", "the name contains a colon"),
        ("/etc/cron.d/x", "the path is absolute"),
        ("/x.txt", "the path is absolute"),
        ("..", "the path climbs out of its folder with '..'"),
        ("../evil.bat", "the path climbs out of its folder with '..'"),
        ("out/../../evil.bat", "the path climbs out of its folder with '..'"),
        ("out/sub/..", "the path climbs out of its folder with '..'"),
    ],
)
def test_each_rule_refuses_with_its_exact_reason(path: str, expected: str) -> None:
    assert produced_file_path_violation(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "out/report.md",
        "a..b.txt",
        "charts/2026/q3/sales.png",
        "intermediate/df.parquet",
        "report v2 (final).pdf",
        "..hidden",
        "trailing..",
        "café/文件.txt",
    ],
)
def test_ordinary_names_are_accepted(path: str) -> None:
    assert produced_file_path_violation(path) is None
