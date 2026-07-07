"""Integration — the image-runtime connect path, end-to-end (Spec N6, T5a; R4-C1-21 close).

The acceptance-#4 proof: a persona assigned an IMAGE-runtime MCP server is resolved THROUGH
the per-tenant runtime to a real ``/mcp`` URL, connected via the EXISTING N4 client UNCHANGED,
and its tool reaches the persona's model-callable toolbox AND dispatches — the fix for
"turned it on but the model can't see it".

The runtime substrate is faked (returns a real loopback ``/mcp`` URL), but everything above it
is real: a real FastMCP server over Streamable HTTP, the real N4 :class:`MCPClient`, the real
``build_default_toolbox``, and a real ``Toolbox.dispatch``. Needs Postgres + ``persona_app``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolCall
from persona.tools._factory import build_default_toolbox
from persona.tools.mcp.catalog import MCPServerCatalogEntry
from persona.tools.mcp.client import MCPClient
from persona_api.config import APIConfig
from persona_api.mcp.runtime import MCPRuntimeInstance
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import catalog_service
from persona_api.services.runtime_factory import RuntimeFactory
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration

_KEY = Fernet.generate_key().decode()
_CONFIG = APIConfig(mcp_credential_key=_KEY)


class _FakeEmbedder:
    model_name = "fake"
    dimension = 384

    def encode(self, _texts: object) -> list[list[float]]:
        return []


class _SpyTierRegistry:
    async def aclose(self) -> None:
        return None


class _FakeRuntime:
    """Returns a fixed ``/mcp`` endpoint (a real loopback server) for any ensure."""

    def __init__(self, endpoint_url: str) -> None:
        self._endpoint = endpoint_url
        self.ensured: list[str] = []

    async def ensure(
        self, *, owner_id: str, server_id: str, image: str, secret_env: Mapping[str, str]
    ) -> MCPRuntimeInstance:
        del image, secret_env
        self.ensured.append(server_id)
        return MCPRuntimeInstance(
            owner_id=owner_id,
            server_id=server_id,
            fly_machine_name="opmcp-x",
            fly_machine_id="m-1",
            endpoint_url=self._endpoint,
            state="running",
        )

    async def stop(self, *, owner_id: str, server_id: str) -> None:
        del owner_id, server_id

    async def reap_idle(self, *, now: object, idle_timeout_s: float) -> int:
        del now, idle_timeout_s
        return 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(port: int, *, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise TimeoutError(f"MCP server never opened port {port}")


@pytest.fixture
def flights_server_url(tmp_path: Path) -> Iterator[str]:
    """A real FastMCP Streamable-HTTP server standing in for the image's in-Machine bridge."""
    port = _free_port()
    # Pin the venv explicitly (VIRTUAL_ENV + venv/bin on PATH) so the spawned server binds
    # regardless of how the suite was launched — inheriting os.environ alone is flaky.
    venv = Path(sys.prefix)
    env = {
        **os.environ,
        "VIRTUAL_ENV": str(venv),
        "PATH": f"{venv / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    log = tmp_path / "server.log"
    with log.open("w") as sink:
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, trusted interpreter
            [
                sys.executable,
                "-m",
                "persona.tools.mcp.builtin",
                "calculator",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=env,
        )
    try:
        try:
            _wait_for_port(port)
        except TimeoutError:  # pragma: no cover — surface the server log on a spawn failure
            proc.terminate()
            pytest.fail(f"MCP server did not start; log:\n{log.read_text()}")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover — defensive
            proc.kill()


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    _ = migrated_engine
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url)
    yield engine
    engine.dispose()


@contextmanager
def _acting_as(owner: str) -> Iterator[None]:
    token = current_user_id.set(owner)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _seed_persona_with_image_server(superuser: Engine, *, owner: str, persona_id: str) -> None:
    """Seed a user, a persona, and an assigned IMAGE-runtime server row (no secret)."""
    with superuser.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, :e) ON CONFLICT DO NOTHING"),
            {"o": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x')"),
            {"p": persona_id, "o": owner},
        )
        sid = conn.execute(
            text(
                "INSERT INTO user_mcp_servers "
                "(owner_id, name, url, auth_method, catalog_source, enabled) "
                "VALUES (:o, 'google-flights', 'image://mcp/google-flights', 'none', "
                "'google-flights', true) RETURNING id"
            ),
            {"o": owner},
        ).scalar_one()
        conn.execute(
            text("INSERT INTO persona_mcp_assignments (persona_id, server_id) VALUES (:p, :s)"),
            {"p": persona_id, "s": sid},
        )


def _catalog_entry() -> MCPServerCatalogEntry:
    return MCPServerCatalogEntry(
        name="google-flights",
        description="flights",
        kind="external",
        risk="medium",
        server_type="server",  # ← image-runtime discriminator
        image="mcp/google-flights",
        source_commit="abc123",
    )


def _persona() -> Persona:
    return Persona(
        persona_id="p1",
        identity=PersonaIdentity(name="T", role="R", background="a test persona for N6 T5a."),
        tools=["mcp:google-flights"],
    )


def _factory(app_engine: Engine, runtime: _FakeRuntime, tmp: Path) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=app_engine,
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=_SpyTierRegistry(),  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=tmp,
        api_config=_CONFIG,
        mcp_runtime=runtime,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_assigned_image_server_tool_reaches_the_model_and_dispatches(
    app_engine: Engine,
    migrated_engine: Engine,
    flights_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_persona_with_image_server(migrated_engine, owner="owner_a", persona_id="p1")
    # The catalog carries google-flights as an image-runtime entry (mirror not shipped in-tree).
    monkeypatch.setattr(catalog_service, "merged_mcp_catalog", lambda: [_catalog_entry()])

    runtime = _FakeRuntime(flights_server_url)
    factory = _factory(app_engine, runtime, tmp_path)
    persona = _persona()

    from persona_api.sandbox import (
        SandboxRequestContext,
        reset_sandbox_request_context,
        set_sandbox_request_context,
    )

    ctx_token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="owner_a", conversation_id="c1")
    )
    try:
        with _acting_as("owner_a"):
            # 1) The connect path resolves the assigned image server through the runtime.
            clients = await factory._build_image_runtime_mcp_clients(persona)
            assert len(runtime.ensured) == 1  # the runtime WAS asked to spawn it
            assert len(clients) == 1

            # 2) The clients ride the EXISTING N4 path into the toolbox, UNCHANGED.
            toolbox, mcp_clients = await build_default_toolbox(
                factory._core_config, persona, extra_mcp_clients=clients
            )
    finally:
        reset_sandbox_request_context(ctx_token)

    try:
        # 3) The image server's tool is now on the persona's model-callable surface...
        names = toolbox.names()
        tool_name = next(n for n in names if n.startswith("mcp:google-flights:"))
        # 4) ...and the model can actually invoke it (real dispatch → real result).
        result = await toolbox.dispatch(ToolCall(name=tool_name, args={"expression": "2 + 3"}))
        assert not result.is_error
        assert "5" in result.content
    finally:
        for c in mcp_clients:
            await c.disconnect()


@pytest.mark.asyncio
async def test_no_runtime_yields_no_image_clients(
    app_engine: Engine, migrated_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Community / CLI / unconfigured: the runtime is absent → image servers simply don't connect
    # (the not-connected signal, T6, reports them). The BYO path must not try to connect them.
    _seed_persona_with_image_server(migrated_engine, owner="owner_a", persona_id="p1")
    monkeypatch.setattr(catalog_service, "merged_mcp_catalog", lambda: [_catalog_entry()])
    factory = RuntimeFactory(
        rls_engine=app_engine,
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=_SpyTierRegistry(),  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=tmp_path,
        api_config=_CONFIG,
        mcp_runtime=None,  # unconfigured
    )
    with _acting_as("owner_a"):
        clients = await factory._build_image_runtime_mcp_clients(_persona())
        # And the BYO path skips it too (its placeholder image:// URL is never SSRF-connected).
        byo = factory._build_byo_mcp_clients(_persona())
    assert clients == []
    assert all(isinstance(c, MCPClient) for c in byo)
    assert all(c.server_name != "google-flights" for c in byo)
