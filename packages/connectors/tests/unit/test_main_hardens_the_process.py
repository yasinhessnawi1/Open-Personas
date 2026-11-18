"""The connector service hardens itself before it runs (R9-258, D-ORCH-11-18-g).

``python -m persona_connectors`` builds a RuntimeFactory, and so a
BuiltinMCPSupervisor whose built-in MCP children run as this process's uid. Like the
api, it holds every secret in its env, so it must be non-dumpable before anything
else runs.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from persona_connectors import __main__ as entry

if TYPE_CHECKING:
    import pytest


def test_the_entry_point_hardens_the_process_first(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _hook() -> bool:
        calls.append("hook")
        return True

    def _run(coro: object) -> None:
        calls.append("run")
        close = getattr(coro, "close", None)
        if close is not None:
            close()  # never awaited here; close it so no warning leaks

    monkeypatch.setattr(entry, "make_non_dumpable", _hook)
    monkeypatch.setattr(asyncio, "run", _run)
    entry.main()
    assert calls == ["hook", "run"]
