"""Unit tests for the MCP catalog auto-sync task + worker wiring (Spec N2, T3).

No DB / no network: the leader gate is a fake (the ``leader_factory`` seam) and
``reconcile_mirror`` is monkeypatched, so we exercise the leader-gating, fail-soft, and
cadence logic without Postgres or a git clone.
"""

# ruff: noqa: SLF001 — exercising private loop internals directly.
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from persona.jobs import JobRegistry
from persona.tools.mcp.mirror_reconcile import MirrorSyncResult
from persona_api.config import APIConfig
from persona_api.jobs import Worker
from persona_api.jobs import catalog_sync as catalog_sync_module
from persona_api.jobs.catalog_sync import CatalogSyncTask, build_catalog_sync

if TYPE_CHECKING:
    import pytest


class _FakeLeader:
    """A fake :class:`LeaderGate` recording whether it was acquired + resigned."""

    def __init__(self, *, wins: bool) -> None:
        self._wins = wins
        self.resigned = False

    def try_become_leader(self) -> bool:
        return self._wins

    def resign(self) -> None:
        self.resigned = True


def _task(*, wins: bool, leaders: list[_FakeLeader]) -> CatalogSyncTask:
    def _factory() -> _FakeLeader:
        leader = _FakeLeader(wins=wins)
        leaders.append(leader)
        return leader

    return CatalogSyncTask(
        dispatch_engine=MagicMock(),
        mirror_path=Path("/tmp/persona-mirror.json"),
        leader_factory=_factory,
    )


# --------------------------------------------------------------------------- #
# CatalogSyncTask.run_once — leader gate + fail-soft                           #
# --------------------------------------------------------------------------- #


def test_run_once_no_op_when_not_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _boom(**_: object) -> MirrorSyncResult:
        nonlocal called
        called = True
        raise AssertionError("reconcile must NOT run when not leader")

    monkeypatch.setattr(catalog_sync_module, "reconcile_mirror", _boom)
    leaders: list[_FakeLeader] = []
    result = _task(wins=False, leaders=leaders).run_once()
    assert result is None
    assert called is False
    # A non-leader never acquires, so there is nothing to resign.
    assert leaders[0].resigned is False


def test_run_once_reconciles_and_resigns_when_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = MirrorSyncResult(added=("a",), updated=(), removed=("b",), total=3)
    monkeypatch.setattr(catalog_sync_module, "reconcile_mirror", lambda **_: expected)
    leaders: list[_FakeLeader] = []
    result = _task(wins=True, leaders=leaders).run_once()
    assert result == expected
    assert leaders[0].resigned is True  # leadership released after the run


def test_run_once_fail_soft_on_reconcile_error_still_resigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(**_: object) -> MirrorSyncResult:
        raise FileNotFoundError("clone produced no servers/ dir")

    monkeypatch.setattr(catalog_sync_module, "reconcile_mirror", _raise)
    leaders: list[_FakeLeader] = []
    result = _task(wins=True, leaders=leaders).run_once()
    assert result is None  # fail-soft: caught, last-good mirror preserved
    assert leaders[0].resigned is True  # the lock is always released


# --------------------------------------------------------------------------- #
# build_catalog_sync — the opt-out (N2-D-3)                                    #
# --------------------------------------------------------------------------- #


def test_build_catalog_sync_disabled_returns_none() -> None:
    config = APIConfig(mcp_catalog_sync_enabled=False)
    assert build_catalog_sync(config, dispatch_engine=MagicMock()) is None


def test_build_catalog_sync_enabled_builds_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONA_MCP_MIRROR_PATH", str(tmp_path / "mirror.json"))
    config = APIConfig(mcp_catalog_sync_enabled=True)
    task = build_catalog_sync(config, dispatch_engine=MagicMock())
    assert isinstance(task, CatalogSyncTask)


def test_build_catalog_sync_without_mirror_path_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    """R9-011: no PERSONA_MCP_MIRROR_PATH → the sync is skipped (warn), NOT pointed at
    the bundled package-data snapshot (a committed file the sync must never rewrite)."""
    monkeypatch.delenv("PERSONA_MCP_MIRROR_PATH", raising=False)
    config = APIConfig(mcp_catalog_sync_enabled=True)
    assert build_catalog_sync(config, dispatch_engine=MagicMock()) is None


# --------------------------------------------------------------------------- #
# Worker._maybe_run_catalog_sync — additive, cadence-gated, offloaded         #
# --------------------------------------------------------------------------- #


def _worker(**kw: object) -> Worker:
    return Worker(
        dispatch_engine=MagicMock(),
        rls_engine=MagicMock(),
        registry=JobRegistry(),
        worker_id="w-test",
        **kw,  # type: ignore[arg-type]
    )


def test_maybe_run_catalog_sync_no_op_when_unwired() -> None:
    worker = _worker()  # no catalog_sync wired
    asyncio.run(worker._maybe_run_catalog_sync())  # must not raise


def test_maybe_run_catalog_sync_runs_then_respects_cadence() -> None:
    sync = MagicMock()
    sync.run_once = MagicMock(return_value=None)
    worker = _worker(catalog_sync=sync, catalog_sync_interval_seconds=10_000.0)

    asyncio.run(worker._maybe_run_catalog_sync())
    assert sync.run_once.call_count == 1  # first eligible iteration fires

    asyncio.run(worker._maybe_run_catalog_sync())
    assert sync.run_once.call_count == 1  # within the interval → skipped (no re-pull)


def test_maybe_run_catalog_sync_failure_does_not_crash_the_loop() -> None:
    sync = MagicMock()
    sync.run_once = MagicMock(side_effect=RuntimeError("boom"))
    worker = _worker(catalog_sync=sync, catalog_sync_interval_seconds=10_000.0)
    # The loop guard swallows the failure; the cadence still advances (retry next interval).
    asyncio.run(worker._maybe_run_catalog_sync())
    assert sync.run_once.call_count == 1
