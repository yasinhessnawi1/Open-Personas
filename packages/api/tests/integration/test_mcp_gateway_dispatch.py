"""Integration proof: a bare ``mcp:docker`` grant reaches the model (Spec N7, T5).

The gateway sibling of ``test_mcp_enablement_expansion.py`` (T1c), which proved the
bare-grant expansion end-to-end for the BUILTIN-launcher source (a real spawned
``time`` subprocess). T1b's own unit tests already prove the gateway gate fix at the
core layer with a fake client (``test_tools_factory.py``); this file closes the
API-INTEGRATION gap the N7 Phase-3 breakdown named explicitly — "(b) gateway
bare-grant -> dispatch (fake gateway)" — by driving the SAME real composition layer
T1c did (a persona ROW seeded into Postgres, loaded through
:meth:`RuntimeFactory._load_persona`, built through :meth:`RuntimeFactory._build_toolbox`
— the exact layer every chat/agentic turn goes through), but for the gateway source
instead of the builtin launcher.

**Scripted transport, not a real Docker MCP Gateway.** Spinning up a real Docker MCP
Gateway in CI is out of scope (it needs an actual Docker daemon + `docker mcp gateway
run`); the mechanism under test is Persona's CONNECT-ONLY client composition
(``_build_gateway_client`` / T1b's ``references_gateway`` bare-form fix), not the
gateway binary itself. So the wire transport is patched exactly like N1's own
adversarial isolation proof (``test_gateway_credential_isolation.py``): a fake
``streamablehttp_client`` + a fake ``mcp.ClientSession`` exposing one tool and
answering one call — real MCP SDK objects and real ``Toolbox``/``MCPClient`` code on
both sides of that seam, only the wire itself is scripted.

Two proofs:
1. The bare-grant + gateway-configured case dispatches for real (the M1-T8-style
   positive pin — this is what "enable via the toggle" actually produces).
2. Fail-closed pin: a persona that never grants the gateway (no ``mcp:docker`` /
   ``mcp:docker:<tool>`` entry), even with the gateway fully configured, never opens
   the gateway transport at all — the ``references_gateway`` gate holds at THIS layer
   too, not just in the core-unit test that first proved it.
"""

# ruff: noqa: ANN401 — the MCP SDK fakes are intentionally Any-typed (mirrors
# test_gateway_credential_isolation.py's scripted-transport shape)
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from persona.config import PersonaCoreConfig
from persona.schema.tools import ToolCall
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.runtime_factory import RuntimeFactory
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_GATEWAY_URL = "http://gw.internal:8811/mcp"

_DOCKER_GRANT_YAML = """\
schema_version: "1.0"
identity:
  name: Dockworker
  role: assistant
  background: |
    A helper whose owner enabled a gateway app via the apps toggle.
  language_default: en
  constraints: []
tools:
  - mcp:docker
"""

_NO_GATEWAY_GRANT_YAML = """\
schema_version: "1.0"
identity:
  name: Landlocked
  role: assistant
  background: |
    A helper who never enabled anything gateway-shaped.
  language_default: en
  constraints: []
tools:
  - file_read
"""

#: Whether the scripted gateway transport was ever opened this test (the load-bearing
#: signal for the fail-closed pin — "never opens the transport", not just "no tools").
_transport_opened: list[bool] = []


@asynccontextmanager
async def _scripted_transport(_url: str, **_kwargs: Any) -> Any:
    _transport_opened.append(True)
    yield (object(), object(), object())


@asynccontextmanager
async def _scripted_session(_r: Any, _w: Any) -> Any:
    yield SimpleNamespace(
        initialize=AsyncMock(),
        list_tools=AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search",
                        description="Search via the gateway.",
                        inputSchema={"type": "object"},
                    )
                ]
            )
        ),
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(text="gateway result: 1 hit")],
                structuredContent=None,
                isError=False,
            )
        ),
    )


@pytest.fixture
def scripted_gateway_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the MCP SDK's wire layer only — real client/Toolbox code either side."""
    import mcp
    import mcp.client.streamable_http as shttp

    _transport_opened.clear()
    monkeypatch.setattr(shttp, "streamablehttp_client", _scripted_transport)
    monkeypatch.setattr(mcp, "ClientSession", _scripted_session)


def _seed_persona_row(database_url: str, *, owner: str, persona_id: str, yaml_text: str) -> None:
    """Insert a (users, personas) pair directly via the superuser DSN (RLS bypass)."""
    su = make_rls_engine(database_url)
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y)"),
            {"i": persona_id, "o": owner, "y": yaml_text},
        )
    su.dispose()


def _cleanup_owner(database_url: str, *, owner: str) -> None:
    """Delete the owner row; ``ON DELETE CASCADE`` takes its personas with it."""
    su = make_rls_engine(database_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
    su.dispose()


def _make_factory(rls_engine: Engine, *, core_config: PersonaCoreConfig) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=rls_engine,
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-n7-t5-gw-audit"),  # noqa: S108 — the M1-T8 audit-root idiom
        core_config=core_config,
    )


async def _teardown(
    factory: RuntimeFactory, rls_engine: Engine, su_url: str, *, owner: str
) -> None:
    for client in factory._mcp_clients:  # noqa: SLF001 — test-only reach into accumulated clients
        await client.disconnect()
    await factory._builtin_mcp.aclose()  # noqa: SLF001 — no builtins spawned here, but symmetric
    rls_engine.dispose()
    _cleanup_owner(su_url, owner=owner)


@pytest.mark.usefixtures("scripted_gateway_transport")
async def test_bare_docker_grant_dispatches_through_the_real_composition_layer(
    migrated_engine: Engine,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """The apps-toggle shape for a gateway app, proven end-to-end at the API layer.

    A persona whose DB-stored YAML declares the bare ``mcp:docker`` grant (what the
    apps chooser toggle writes for an app served through the operator's gateway) is
    loaded through the real ``RuntimeFactory`` composition path with a gateway URL
    configured; T1b's expansion pass turns that bare grant into the gateway's real
    ``mcp:docker:search`` tool name, and one real dispatch through the scripted
    transport returns a live result.
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_n7_t5_gw", "persona_n7_t5_gw_dispatch"
    _seed_persona_row(su_url, owner=owner, persona_id=persona_id, yaml_text=_DOCKER_GRANT_YAML)

    rls_engine = make_rls_engine(app_url)
    factory = _make_factory(
        rls_engine,
        core_config=PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            docker_mcp_gateway_url=_GATEWAY_URL,
        ),
    )
    token = current_user_id.set(owner)
    try:
        persona = factory._load_persona(persona_id)  # noqa: SLF001 — the real YAML->Persona load
        assert persona.tools == ["mcp:docker"], "sanity: the bare grant survived the DB round-trip"

        toolbox = await factory._build_toolbox(persona, scanned_skills=[])  # noqa: SLF001

        assert _transport_opened, "the gateway transport was never opened"
        names = toolbox.names()  # type: ignore[attr-defined]
        assert "mcp:docker:search" in names, (
            f"the gateway's advertised tool must reach the model; got {names!r}"
        )
        assert toolbox.is_allowed("mcp:docker:search")  # type: ignore[attr-defined]
        # The bare grant is the documented allow-list form but no tool ever
        # registers under it — it is never itself advertised (byte-for-byte T1b).
        assert "mcp:docker" not in names

        result = await toolbox.dispatch(  # type: ignore[attr-defined]
            ToolCall(name="mcp:docker:search", args={"q": "hello"}, call_id="n7-t5-gw-1")
        )
        assert result.is_error is False, result.content
        assert "1 hit" in result.content
    finally:
        current_user_id.reset(token)
        await _teardown(factory, rls_engine, su_url, owner=owner)


@pytest.mark.usefixtures("scripted_gateway_transport")
async def test_no_gateway_grant_never_opens_the_gateway_transport(
    migrated_engine: Engine,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """Fail-closed pin (M1-T8 precedent): un-granted stays un-connected.

    Even with the gateway fully configured, a persona that never declares
    ``mcp:docker`` (bare or 3-segment) never causes the gateway transport to open at
    all — T1b's ``references_gateway`` bare-form gate holds at this real-composition
    layer too, not merely in the core-unit test that first proved it. Composition
    still completes cleanly for the persona's other declared tool.
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    su_url = os.environ["DATABASE_URL"]
    owner, persona_id = "user_n7_t5_gw", "persona_n7_t5_gw_ungranted"
    _seed_persona_row(su_url, owner=owner, persona_id=persona_id, yaml_text=_NO_GATEWAY_GRANT_YAML)

    rls_engine = make_rls_engine(app_url)
    factory = _make_factory(
        rls_engine,
        core_config=PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            docker_mcp_gateway_url=_GATEWAY_URL,
        ),
    )
    token = current_user_id.set(owner)
    try:
        persona = factory._load_persona(persona_id)  # noqa: SLF001
        toolbox = await factory._build_toolbox(persona, scanned_skills=[])  # noqa: SLF001

        assert not _transport_opened, "the gateway transport must never open when un-granted"
        names = toolbox.names()  # type: ignore[attr-defined]
        assert not any(n.startswith("mcp:docker") for n in names)
        assert "file_read" in names, "the turn still composes cleanly for the persona's other tool"
    finally:
        current_user_id.reset(token)
        await _teardown(factory, rls_engine, su_url, owner=owner)
