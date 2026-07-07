"""Unit — the Fly runtime config + the background idle-reaper lifecycle (Spec N6, T5b).

``FlyRuntimeConfig.configured`` is the "is the runtime on?" signal the startup guard reads;
the reaper's ``start``/``aclose`` mirror SandboxPool's single-ownership task lifecycle.
"""

from __future__ import annotations

import asyncio

import pytest
from persona_api.mcp.fly_runtime import FlyPerTenantMCPRuntime
from persona_api.mcp.runtime_config import FlyRuntimeConfig


class TestFlyRuntimeConfig:
    def test_unconfigured_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("PERSONA_MCP_RUNTIME_FLY_APP", "FLY_API_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        cfg = FlyRuntimeConfig()
        assert cfg.configured is False

    def test_requires_both_app_and_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_MCP_RUNTIME_FLY_APP", "op-mcp")
        monkeypatch.delenv("FLY_API_TOKEN", raising=False)
        assert FlyRuntimeConfig().configured is False  # app but no token
        monkeypatch.setenv("FLY_API_TOKEN", "tok")
        assert FlyRuntimeConfig().configured is True  # both → on

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in (
            "PERSONA_MCP_RUNTIME_FLY_PORT",
            "PERSONA_MCP_RUNTIME_IDLE_TIMEOUT_S",
            "PERSONA_MCP_RUNTIME_REAP_INTERVAL_S",
            "PERSONA_MCP_RUNTIME_MAX_PER_TENANT",
        ):
            monkeypatch.delenv(var, raising=False)
        cfg = FlyRuntimeConfig()
        assert cfg.port == 8000
        assert cfg.idle_timeout_s == 300.0
        assert cfg.reap_interval_s == 60.0
        assert cfg.max_per_tenant == 3

    def test_token_is_not_reprd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FLY_API_TOKEN", "super-secret")
        cfg = FlyRuntimeConfig()
        assert "super-secret" not in repr(cfg)


def _runtime() -> FlyPerTenantMCPRuntime:
    # Stub engines/fly — the reaper interval is huge so reap_idle never fires before aclose,
    # so those collaborators are never touched (we test the task lifecycle only).
    return FlyPerTenantMCPRuntime(
        rls_engine=object(),  # type: ignore[arg-type]
        bypass_engine=object(),  # type: ignore[arg-type]
        fly=object(),  # type: ignore[arg-type]
        app="op",
        port=8000,
        is_runnable_image=lambda _i: True,
        reap_interval_s=10_000.0,
    )


class TestReaperLifecycle:
    @pytest.mark.asyncio
    async def test_start_spawns_and_aclose_cancels(self) -> None:
        rt = _runtime()
        await rt.start()
        assert rt._reaper_task is not None
        assert not rt._reaper_task.done()
        await rt.aclose()
        assert rt._reaper_task.done()

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self) -> None:
        rt = _runtime()
        await rt.start()
        first = rt._reaper_task
        await rt.start()  # no second task
        assert rt._reaper_task is first
        await rt.aclose()

    @pytest.mark.asyncio
    async def test_aclose_is_idempotent(self) -> None:
        rt = _runtime()
        await rt.start()
        await rt.aclose()
        await rt.aclose()  # no raise

    @pytest.mark.asyncio
    async def test_start_after_close_raises(self) -> None:
        rt = _runtime()
        await rt.start()
        await rt.aclose()
        with pytest.raises(Exception, match="closed"):
            await rt.start()

    @pytest.mark.asyncio
    async def test_reaper_survives_a_sweep_error(self) -> None:
        # A reap sweep that raises must NOT kill the loop (SandboxPool discipline).
        rt = _runtime()
        calls = {"n": 0}

        async def _boom(*, now: object, idle_timeout_s: float) -> int:
            del now, idle_timeout_s
            calls["n"] += 1
            raise RuntimeError("sweep boom")

        rt.reap_idle = _boom  # type: ignore[method-assign]
        rt._reap_interval_s = 0.01  # fire quickly
        await rt.start()
        await asyncio.sleep(0.05)  # let a couple of sweeps fire + fail
        assert calls["n"] >= 1  # it kept looping despite the error
        assert not rt._reaper_task.done()  # loop still alive
        await rt.aclose()
