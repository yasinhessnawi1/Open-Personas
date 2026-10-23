"""A built-in MCP uid set on Windows is reported, not silently ignored (Spec WIN review INFO).

Windows has no setuid, so ``PERSONA_MCP_BUILTIN_UID`` cannot drop the children's
privileges there; the supervisor says so at start instead of looking effective.
"""

from __future__ import annotations

import sys

import pytest
from loguru import logger
from persona_api.mcp.builtin_launcher import BuiltinMCPSupervisor


def _warnings_while(build: object) -> list[str]:
    seen: list[str] = []
    sink = logger.add(lambda message: seen.append(str(message)), level="WARNING")
    try:
        build()  # type: ignore[operator]
    finally:
        logger.remove(sink)
    return seen


def test_a_uid_on_windows_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    seen = _warnings_while(lambda: BuiltinMCPSupervisor(enabled=(), child_uid=1500))
    assert any("PERSONA_MCP_BUILTIN_UID is set but Windows" in line for line in seen)


def test_no_uid_or_a_posix_platform_says_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert not _warnings_while(lambda: BuiltinMCPSupervisor(enabled=()))
    monkeypatch.setattr(sys, "platform", "linux")
    seen = _warnings_while(lambda: BuiltinMCPSupervisor(enabled=(), child_uid=1500))
    assert not any("PERSONA_MCP_BUILTIN_UID" in line for line in seen)
