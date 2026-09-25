"""Portability guard: no platform-specific strftime flag in product source (R9-225).

Python's ``strftime`` hands the format to the platform C library, and its flags are not
portable. ``%-d``, ``%_d`` and ``%^a`` are glibc extensions; ``%#d`` is the Windows
spelling of "no padding", which glibc accepts but gives another meaning. Across platforms
they behave differently or raise ``ValueError: Invalid format string``: on Windows all
three glibc flags raise. CI runs on Linux, so a ``%-d`` stays green there while every
Windows install crashes on it, which is how the chat reschedule echo shipped broken in 1.2.1.

This test reads every string literal in ``packages/*/src`` (f-string format specs
included, since that is where the shipped one lived) and fails on any of those flags in
front of a strftime directive, naming each file and line. Comments are not string literals,
so a comment may still name the flag it warns about. Build an unpadded number from the
datetime field (``dt.day``) instead.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

#: ``%%`` is matched first so an escaped percent is consumed whole and ``%%-d`` (the
#: literal text "%-d") is not mistaken for a flag. The flags are glibc's ``-``, ``_`` and
#: ``^`` plus Windows' ``#``. Only the directives Python documents for strftime may follow:
#: glibc-only directives (``%k``, ``%l``, ``%P``, ``%s``) are left out on purpose, because
#: ``%-s`` is an ordinary printf-style left-justified string and would bury real findings.
#: Prose such as "44%-of-tasks" is not a finding either.
_FLAG_OR_ESCAPE = re.compile(r"%%|%[-#_^][aAbBcdfGHIjmMpSuUVwWxXyYzZ]")

#: A floor, not a count: high enough that a broken scan (wrong root, renamed layout)
#: cannot pass as a clean one. The tree had 817 modules when this landed.
_MIN_MODULES_SCANNED = 500

#: Every package with Python source. Each must contribute to the scan, so losing a whole
#: package (a renamed ``src`` layout, say) cannot pass as a clean scan behind the floor.
_PACKAGES = frozenset({"api", "connectors", "core", "runtime", "voice"})

#: The module that shipped the defect. A scan that cannot see it cannot see the next one.
_SHIPPED_OFFENDER = "packages/runtime/src/persona_runtime/task_origination/reschedule_flow.py"

#: Written out here rather than read from the pattern, so narrowing the pattern fails a case.
_FLAGS = "-#_^"
_DOCUMENTED_DIRECTIVES = "aAbBcdfGHIjmMpSuUVwWxXyYzZ"


def _repo_root() -> Path:
    """The worktree root, found by walking up to the directory holding ``packages/``."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "packages").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    msg = "could not locate the repository root from the test file"
    raise AssertionError(msg)


def _platform_only_flags(text: str) -> list[str]:
    """Every platform-specific strftime flag in ``text``, escaped percents skipped."""
    return [m.group() for m in _FLAG_OR_ESCAPE.finditer(text) if m.group() != "%%"]


def _offending_lines(source: str, filename: str = "<string>") -> list[tuple[int, str]]:
    """``(line, flag)`` for each platform-specific flag inside a string literal of ``source``."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend((node.lineno, flag) for flag in _platform_only_flags(node.value))
    return sorted(found)


@pytest.mark.parametrize(
    "spec", [f"%{flag}{letter}" for flag in _FLAGS for letter in _DOCUMENTED_DIRECTIVES]
)
def test_every_flag_is_found_before_every_documented_directive(spec: str) -> None:
    assert _offending_lines(f'x = "{spec}"\n') == [(1, spec)]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('x = f"{dt:%a %-d %b}"\n', [(1, "%-d")]),
        ('x = dt.strftime("%#d/%m")\n', [(1, "%#d")]),
        ('x = 1\ny = "{:%-H}".format(dt)\n', [(2, "%-H")]),
        ('x = dt.strftime("%-m/%-I %-M")\n', [(1, "%-I"), (1, "%-M"), (1, "%-m")]),
        ('x = f"{dt:%_d %^a}"\n', [(1, "%^a"), (1, "%_d")]),
        ('x = f"{dt:%a} {dt.day} {dt:%b}"\n', []),
        ('x = "%%-d is the literal text"\n', []),
        ('x = "best in 44%-of-tasks"\n', []),
        ("# a comment naming %-d is fine\nx = 1\n", []),
    ],
)
def test_the_scan_finds_platform_only_flags_and_nothing_else(
    source: str, expected: list[tuple[int, str]]
) -> None:
    assert _offending_lines(source) == expected


@pytest.mark.parametrize("spec", ["%-s", "%-5d", "%-k", "%-l", "%-P"])
def test_printf_specs_and_glibc_only_directives_are_not_findings(spec: str) -> None:
    assert _offending_lines(f'x = "{spec}"\n') == []


def test_a_module_that_does_not_parse_is_named_in_the_error() -> None:
    with pytest.raises(SyntaxError) as raised:
        _offending_lines("x = (\n", filename="packages/x/src/broken.py")
    assert raised.value.filename == "packages/x/src/broken.py"


def test_no_product_source_uses_a_platform_only_strftime_flag() -> None:
    root = _repo_root()
    modules = sorted(root.glob("packages/*/src/**/*.py"))
    scanned = {path.relative_to(root).as_posix() for path in modules}
    assert len(modules) >= _MIN_MODULES_SCANNED, (
        f"scanned only {len(modules)} modules under {root / 'packages'}; the scan is broken"
    )
    missing = _PACKAGES - {rel.split("/")[1] for rel in scanned}
    assert not missing, f"no modules scanned from packages {sorted(missing)}; the scan is broken"
    assert _SHIPPED_OFFENDER in scanned, f"{_SHIPPED_OFFENDER} was not scanned; the scan is broken"
    offenders = [
        f"{path.relative_to(root).as_posix()}:{line} uses {flag}"
        for path in modules
        for line, flag in _offending_lines(path.read_text(encoding="utf-8"), filename=str(path))
    ]
    assert offenders == [], (
        "platform-specific strftime flags are not portable (R9-225); build the unpadded "
        "number from the datetime field instead:\n" + "\n".join(offenders)
    )
