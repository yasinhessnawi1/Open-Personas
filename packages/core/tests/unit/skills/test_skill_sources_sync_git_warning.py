"""Unit tests for the skill-source clone's missing-``git`` diagnostic (N7-T4b, R9-041 rider).

``clone_at_ref`` shells out to ``git`` (mirrors N1's ``_clone_registry``). A
runtime image missing the binary previously failed with a bare
``FileNotFoundError`` from ``subprocess`` — accurate but opaque. One honest
WARNING now fires before the (unchanged) attempt, naming the real cause;
git-present stays byte-identical (silent, still just the clone).
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from loguru import logger as _loguru_logger
from persona.skills.skill_sources_sync import clone_at_ref

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def loguru_capture() -> Iterator[list[str]]:
    """Loguru sink capturing WARNING+ lines (persona.logging wraps loguru, so
    stdlib ``caplog`` sees nothing — the test_api_boot_hermetic.py pattern)."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


def test_missing_git_warns_before_the_still_unchanged_clone_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_capture: list[str],
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    def _raise_missing_binary(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _raise_missing_binary)

    # No behavior change (fail-soft-stands rule): the attempt still happens
    # and still raises — the warning only makes the eventual failure legible,
    # it never substitutes for or swallows it.
    with pytest.raises(FileNotFoundError):
        clone_at_ref("https://example.invalid/skills.git", None, tmp_path / "dest")

    assert any("git not on PATH" in line for line in loguru_capture), loguru_capture


def test_git_present_clones_silently_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    loguru_capture: list[str],
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/git")
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="deadbeef", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    checkout = clone_at_ref("https://example.invalid/skills.git", None, tmp_path / "dest")

    assert calls, "the clone was expected to run"
    assert checkout.commit == "deadbeef"
    assert not any("git not on PATH" in line for line in loguru_capture)
