"""R9-313: the REAL lifespan's teardown ends within its budget, whatever hangs inside it.

Every test leaves a real ``TestClient(app)`` context, so the api's own ``_lifespan`` is what
tears down. The collaborators the lifespan built are patched in place, inside the context, to
hang or to be slow; nothing calls ``aclose`` by hand. The end of the teardown is the engine
dispose, the lifespan's last step, so what is measured is the lifespan, not the event loop's
own teardown after it (which still waits for a task that ignores its cancel).

Pinned:

- a chat turn whose close never ends and a slow agentic-run close (standing in for the
  worker's drain) are closed TOGETHER, and the lifespan still ends within its budget
  (sequential or unbounded closers fail this);
- the connectors' shared HTTP client closes only after the closer group has returned (N1);
- a hung telemetry flush is capped, so the closers and the final phase still get their time
  (N3).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.background import shutdown
from persona_api.config import APIConfig, Edition
from persona_api.services.origination_delivery import ChannelDeliverers

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from persona.stores.embedder import Embedder

_BUDGET_S = 2.0
_PRE_CAP_S = 0.4
_RESERVE_S = 0.4
#: A stuck close ignores every cancel for this long, then gives up, so the event loop's own
#: teardown (which waits for it) ends and the test does not hang on a regression.
_STUCK_FOR_S = 5.0
#: The time the lifespan may take beyond its budget: scheduling, the engine dispose itself.
_SLACK_S = 1.0


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, embedder: Embedder) -> None:
    """No Postgres, no torch, and small phases so the budget is seconds, not twenty."""
    from persona_api.services import persona_service

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    monkeypatch.setattr(persona_service, "default_embedder", lambda *_a, **_k: embedder)
    monkeypatch.setattr(shutdown, "PRE_CLOSE_CAP_S", _PRE_CAP_S)
    monkeypatch.setattr(shutdown, "FINAL_RESERVE_S", _RESERVE_S)


def _config(tmp_path: Path, **overrides: Any) -> APIConfig:  # noqa: ANN401 - passthrough
    return APIConfig(
        edition=Edition.community,
        community_db_path=tmp_path / "community.db",
        community_memory_path=tmp_path / "chroma",
        workspace_root=tmp_path / "work",
        audit_root=str(tmp_path / "audit"),
        shutdown_budget_seconds=_BUDGET_S,
        **overrides,
    )


async def _stuck() -> None:
    """A close whose cancel cannot land, for ``_STUCK_FOR_S``."""
    ends = time.monotonic() + _STUCK_FOR_S
    while time.monotonic() < ends:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(0.05)


def _record(
    monkeypatch: pytest.MonkeyPatch,
    obj: object,
    name: str,
    log: dict[str, float],
    body: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Replace ``obj.<name>`` with a spy that records when it started and ended."""
    real = getattr(obj, name)

    async def _spy() -> None:
        log.setdefault(f"{name} started", time.monotonic())  # the FIRST call counts
        try:
            await (body() if body is not None else real())
        finally:
            log[f"{name} ended"] = time.monotonic()

    monkeypatch.setattr(obj, name, _spy)


def _record_dispose(monkeypatch: pytest.MonkeyPatch, engine: Any, log: dict[str, float]) -> None:  # noqa: ANN401
    real = engine.dispose

    def _spy(*a: Any, **k: Any) -> None:  # noqa: ANN401 - passthrough
        log.setdefault("dispose", time.monotonic())
        real(*a, **k)

    monkeypatch.setattr(engine, "dispose", _spy)


async def _slow_close() -> None:
    await asyncio.sleep(0.6)


def test_stuck_and_slow_closers_close_together_within_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: dict[str, float] = {}
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        state = client.app.state
        assert state.chat_turn_registry is not None
        assert state.run_registry is not None
        _record(monkeypatch, state.chat_turn_registry, "aclose", log, _stuck)
        runs: dict[str, float] = {}
        _record(monkeypatch, state.run_registry, "aclose", runs, _slow_close)
        _record_dispose(monkeypatch, state.rls_engine, log)
        began = time.monotonic()

    assert "aclose started" in log, "the chat turns were never closed"
    assert "aclose ended" in runs, "the slow close was cut short: it fits in its window"
    together = abs(log["aclose started"] - runs["aclose started"])
    assert together < 0.3, f"the closers ran one after another ({together:.2f}s apart)"
    spent = log["dispose"] - began
    assert spent < _BUDGET_S + _SLACK_S, f"the lifespan took {spent:.2f}s, over its budget"


def test_a_hung_telemetry_flush_leaves_the_closers_and_the_final_phase_their_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log: dict[str, float] = {}
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        state = client.app.state
        assert state.telemetry_buffer is not None
        _record(monkeypatch, state.telemetry_buffer, "aclose", log, _stuck)
        chats: dict[str, float] = {}
        _record(monkeypatch, state.chat_turn_registry, "aclose", chats)
        factory = state.build_conversation_loop.__self__
        final: dict[str, float] = {}
        _record(monkeypatch, factory, "aclose", final)
        _record_dispose(monkeypatch, state.rls_engine, log)
        began = time.monotonic()

    assert chats["aclose started"] - began < _PRE_CAP_S + 0.3, "the flush took the closers' time"
    assert "aclose ended" in final, "the final phase got no time"
    assert log["dispose"] - began < _BUDGET_S + _SLACK_S


def _install_connectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real host around a bundle with one long-poll runner (the lifecycle-test seam)."""
    from persona_connectors import service as connector_service
    from persona_connectors.service import ConnectorsBundle

    async def _runner() -> None:
        await asyncio.sleep(3600)

    async def _fake_build(**_kwargs: Any) -> ConnectorsBundle:  # noqa: ANN401 - passthrough
        deliverers = {"telegram": object()}
        channels = ChannelDeliverers()
        channels.bind(deliverers)  # type: ignore[arg-type]
        return ConnectorsBundle(
            deliverers=deliverers,  # type: ignore[arg-type]
            runners={"telegram": _runner},
            http_app=None,
            idle_sweep=None,
            channels=channels,
        )

    monkeypatch.setattr(connector_service, "build_connectors", _fake_build)


def test_the_connector_http_client_closes_only_after_the_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 3 N1: the worker's jobs send through the connectors' client during the drain."""
    _install_connectors(monkeypatch)
    log: dict[str, float] = {}
    app = create_app(_config(tmp_path, embed_connectors=True))
    with TestClient(app) as client:
        state = client.app.state
        handle = state.embedded_connectors
        assert handle is not None
        _record(monkeypatch, handle, "stop_runners", log)
        _record(monkeypatch, handle, "close_http", log)
        # The slow closer stands in for the worker's drain (no worker on SQLite).
        drain: dict[str, float] = {}
        _record(monkeypatch, state.run_registry, "aclose", drain, _slow_close)

    assert log["stop_runners started"] < drain["aclose ended"], "the runners stop alongside it"
    assert log["close_http started"] >= drain["aclose ended"], (
        "the shared client closed while the drain could still send through it"
    )
