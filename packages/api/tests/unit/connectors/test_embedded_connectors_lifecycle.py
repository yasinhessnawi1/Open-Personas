"""The embedded connector runners' REAL lifecycle (Spec I1 T2).

Every assertion here goes through ``TestClient(app)`` as a context manager, so the api's
own ``_lifespan`` is what composes, starts and drains the host. Nothing calls ``start()``
or ``aclose()`` by hand: a test that forces the end state itself would false-green a fix
production cannot reach, and this spec's whole risk is a live socket path.

What is pinned:

- OFF is inert, proven STRUCTURALLY (``persona_connectors`` never even imported);
- ON with no platform configured boots, warns and keeps serving (D-I1-6);
- a genuinely crashing runner is genuinely restarted by the shipped ``_supervised``;
- a persistently crashing runner is given up on while its siblings keep serving;
- leaving the context drains every task, BEFORE the runtime factory closes and before the
  engines are disposed (D-I1-8), asserted as a recorded ORDER rather than a comment.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger as _loguru_logger
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persona.stores.embedder import Embedder


def _community_config(tmp_path: Path, **overrides: Any) -> APIConfig:  # noqa: ANN401 — passthrough
    """A zero-infra community config: SQLite + Chroma, no Postgres, no Clerk.

    Community gives the lifespan a real ``rls_engine`` without Docker, which is what lets
    these tests exercise the embed branch (it self-gates on an engine) hermetically.
    """
    return APIConfig(
        edition=Edition.community,
        community_db_path=tmp_path / "community.db",
        community_memory_path=tmp_path / "chroma",
        workspace_root=tmp_path / "work",
        audit_root=str(tmp_path / "audit"),
        **overrides,
    )


@pytest.fixture
def loguru_warnings() -> Iterator[list[str]]:
    """Capture WARNING+ through loguru.

    ``persona.logging`` wraps loguru, so stdlib ``caplog`` sees nothing. This is the
    established sink pattern from ``test_api_boot_hermetic.py`` / ``test_api_app_factory.py``.
    """
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, embedder: Embedder) -> None:
    """No Postgres, no torch: the boot must not depend on either."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    from persona_api.services import persona_service

    monkeypatch.setattr(persona_service, "default_embedder", lambda *_a, **_k: embedder)


# --------------------------------------------------------------------------- OFF


def test_importing_the_api_never_imports_the_connectors_package() -> None:
    """D-I1-1, the packaging guard, in a FRESH interpreter.

    persona-connectors depends on persona-api, so the reverse edge would be a metadata
    cycle. The api therefore imports it ONLY inside the flag branch, at lifespan time, and
    declares no dependency on it. This has to run in a subprocess: inside this one,
    ``persona_connectors`` is already loaded by the tests below, and deleting it from
    ``sys.modules`` would not undo a module-scope import in ``app.py`` (the name would stay
    bound and nothing would re-import), so an in-process check silently passes through the
    exact bug it is meant to catch.
    """
    probe = (
        "import sys; import persona_api.app;"
        "leaked = sorted(m for m in sys.modules if m.startswith('persona_connectors'));"
        "print('LEAKED:' + ','.join(leaked))"
    )
    result = subprocess.run(  # noqa: S603 — fixed argv, this interpreter
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    leaked = result.stdout.strip().removeprefix("LEAKED:")
    assert leaked == "", (
        f"importing persona_api.app pulled in persona_connectors ({leaked}); the api must "
        "not depend on the package that depends on it. Keep the import inside the "
        "PERSONA_API_EMBED_CONNECTORS branch."
    )


def test_flag_off_boot_leaves_the_connectors_package_unimported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime half of the same invariant: a flag-OFF boot imports nothing.

    The guard above pins the module graph; this pins the LIFESPAN, so an import added
    inside ``_lifespan`` but outside the flag branch is caught too.
    """
    # Snapshot before filtering: the lifespan imports on the TestClient portal thread,
    # so iterating sys.modules live raises "dictionary changed size during iteration".
    for name in [m for m in list(sys.modules) if m.startswith("persona_connectors")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    app = create_app(_community_config(tmp_path))
    with TestClient(app) as client:
        assert client.app.state.embedded_connectors is None
        leaked = [m for m in list(sys.modules) if m.startswith("persona_connectors")]
        assert not leaked, (
            f"a flag-OFF boot imported persona_connectors ({leaked}); the import must stay "
            "inside the branch"
        )


# --------------------------------------------------------------------------- ON, empty


def test_flag_on_with_no_platform_configured_warns_and_keeps_serving(
    tmp_path: Path, loguru_warnings: list[str]
) -> None:
    """D-I1-6: absence degrades this subsystem, it never fails the api's startup.

    A crash-looping api because a connector env var is missing would take the web app down
    for a feature nobody configured. The precedent is the in-process worker's keyless
    disable in the same lifespan. Serving is proven with a real request, not by the absence
    of an exception.
    """
    app = create_app(_community_config(tmp_path, embed_connectors=True))
    with TestClient(app) as client:
        assert client.app.state.embedded_connectors is None
        assert client.get("/livez").status_code == 200
    assert any("NO connector platform is configured" in line for line in loguru_warnings), (
        "the operator must be told loudly; a silent no-op is how a configured-but-absent "
        f"connector goes unnoticed. Captured: {loguru_warnings}"
    )


# ------------------------------------------------------- ON, with real supervised runners


class _CrashedError(RuntimeError):
    """A runner fault that is genuinely raised, never simulated by a flag."""


def _install_fake_bundle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runners: dict[str, Any],
    started: list[str],
) -> None:
    """Make the REAL host compose a bundle whose runners we control.

    The seam is ``build_connectors``: everything downstream of it (``EmbeddedConnectors``,
    ``_supervised``, the lifespan's start and drain) is the shipped code path. Patching the
    module attribute the host reads adds no production surface.
    """
    from persona_connectors import service as connector_service
    from persona_connectors.service import ConnectorsBundle

    async def _fake_build(**kwargs: Any) -> ConnectorsBundle:  # noqa: ANN401 — passthrough
        started.append("built")
        assert isinstance(kwargs["http"], httpx.AsyncClient)
        return ConnectorsBundle(
            deliverers={name: object() for name in runners},  # type: ignore[misc]
            runners=runners,
            http_app=None,
            idle_sweep=None,
        )

    # The host imports this name INSIDE the function, so it resolves from the source
    # module at call time; patching the host module attribute would silently miss.
    monkeypatch.setattr(connector_service, "build_connectors", _fake_build)


def test_a_crashed_runner_is_restarted_by_the_shipped_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9-073c through the REAL lifespan: crash, restart, recover.

    The runner raises for real and the production ``_supervised`` observes it. The backoff
    clock is injected rather than waited on, which removes the WAITING, never the
    transition: the crash, the restart and the recovery all genuinely happen.
    """
    attempts: list[int] = []
    recovered = asyncio.Event()

    async def flaky() -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise _CrashedError("telegram long-poll blew up")
        recovered.set()
        await asyncio.sleep(3600)  # a real runner serves forever until cancelled

    _install_fake_bundle(monkeypatch, runners={"telegram": flaky}, started=[])

    from persona_api.background import connectors_host

    async def _no_wait(_seconds: float) -> None:
        return

    real_init = connectors_host.EmbeddedConnectors.__init__

    def _init_without_backoff(self: Any, bundle: Any, **kwargs: Any) -> None:  # noqa: ANN401
        real_init(self, bundle, **{**kwargs, "sleep": _no_wait})

    monkeypatch.setattr(connectors_host.EmbeddedConnectors, "__init__", _init_without_backoff)

    app = create_app(_community_config(tmp_path, embed_connectors=True))
    with TestClient(app) as client:
        handle = client.app.state.embedded_connectors
        assert handle is not None

        async def _wait() -> None:
            await asyncio.wait_for(recovered.wait(), timeout=5)

        client.portal.call(_wait)  # type: ignore[attr-defined]
        assert attempts == [1, 2], f"expected one crash then one restart, saw {attempts}"


def test_a_persistently_crashing_runner_is_given_up_on_while_siblings_keep_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9-071 containment: one dead platform must not take the others down.

    ``asyncio.gather`` propagates the first exception, which is why containment exists at
    all. Here the crashing runner exhausts the ceiling and stops while the healthy sibling
    is still running, and the api is still answering requests throughout.
    """
    crashes = 0
    healthy_running = asyncio.Event()

    async def always_crashes() -> None:
        nonlocal crashes
        crashes += 1
        raise _CrashedError("discord gateway is permanently broken")

    async def healthy() -> None:
        healthy_running.set()
        await asyncio.sleep(3600)

    _install_fake_bundle(
        monkeypatch, runners={"discord": always_crashes, "telegram": healthy}, started=[]
    )

    from persona_api.background import connectors_host

    async def _no_wait(_seconds: float) -> None:
        return

    real_init = connectors_host.EmbeddedConnectors.__init__

    def _init_without_backoff(self: Any, bundle: Any, **kwargs: Any) -> None:  # noqa: ANN401
        real_init(self, bundle, **{**kwargs, "sleep": _no_wait})

    monkeypatch.setattr(connectors_host.EmbeddedConnectors, "__init__", _init_without_backoff)

    app = create_app(_community_config(tmp_path, embed_connectors=True))
    with TestClient(app) as client:
        handle = client.app.state.embedded_connectors
        assert handle is not None

        async def _wait() -> None:
            await asyncio.wait_for(healthy_running.wait(), timeout=5)
            # Let the crashing runner burn through its ceiling (11 attempts, no real waits).
            for _ in range(200):
                await asyncio.sleep(0)

        client.portal.call(_wait)  # type: ignore[attr-defined]
        # The ceiling is 10 restarts after the initial attempt.
        assert crashes == 11, f"expected the ceiling to stop it at 11 attempts, saw {crashes}"
        # The api is still serving, and the sibling never died with it.
        assert client.get("/livez").status_code == 200
        assert healthy_running.is_set()


# --------------------------------------------------------------------------- drain order


def test_shutdown_drains_the_runners_before_the_factory_closes_and_the_engines_dispose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-I1-8, proven by ORDER through a real shutdown, not asserted in a comment.

    A runner cancelled after the engines are disposed could still be mid-turn against a
    dead pool. Exiting the ``TestClient`` context runs the api's genuine shutdown path;
    the recorded sequence is what pins the position of the drain among its neighbours.
    """
    order: list[str] = []
    cancelled = asyncio.Event()

    async def runner() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            order.append("runner_cancelled")
            cancelled.set()
            raise

    _install_fake_bundle(monkeypatch, runners={"telegram": runner}, started=[])

    app = create_app(_community_config(tmp_path, embed_connectors=True))

    with TestClient(app) as client:
        handle = client.app.state.embedded_connectors
        assert handle is not None

        # Record the neighbours the drain must precede. Patched here, INSIDE the context,
        # so the real objects the lifespan built are the ones being observed.
        factory = client.app.state.build_conversation_loop.__self__
        real_factory_aclose = factory.aclose

        async def _spy_factory_aclose() -> None:
            order.append("runtime_factory_aclose")
            await real_factory_aclose()

        monkeypatch.setattr(factory, "aclose", _spy_factory_aclose)

        engine = client.app.state.rls_engine
        real_dispose = engine.dispose

        def _spy_dispose(*a: Any, **k: Any) -> None:  # noqa: ANN401 — passthrough
            order.append("engine_dispose")
            real_dispose(*a, **k)

        monkeypatch.setattr(engine, "dispose", _spy_dispose)

    assert order == ["runner_cancelled", "runtime_factory_aclose", "engine_dispose"], (
        f"the connector drain must precede the factory close and engine disposal; saw {order}"
    )
