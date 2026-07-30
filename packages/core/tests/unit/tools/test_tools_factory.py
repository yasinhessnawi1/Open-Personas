"""Tests for build_default_toolbox + PersonaCoreConfig MCP parsing (T12)."""

# ruff: noqa: ANN401, ARG001, ARG002, ERA001, SLF001
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona.config import PersonaCoreConfig
from persona.schema.persona import Persona, PersonaIdentity
from persona.tools import build_default_toolbox
from persona.tools.audit import MemoryToolAuditLogger
from persona.tools.toolbox import Toolbox

if TYPE_CHECKING:
    from pathlib import Path


def _persona(*, persona_id: str = "test-persona", tools: list[str] | None = None) -> Persona:
    return Persona(
        persona_id=persona_id,
        identity=PersonaIdentity(
            name="Test",
            role="Tester",
            background="A test persona for the spec-03 factory unit tests.",
        ),
        tools=tools or [],
    )


# Section: PersonaCoreConfig MCP-server parsing (D-03-22)


class TestPersonaCoreConfigMCPParsing:
    def test_empty_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSONA_MCP_SERVERS", raising=False)
        config = PersonaCoreConfig()
        assert config.mcp_servers_parsed == {}

    def test_parses_single_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_MCP_SERVERS", "a=https://a.example/mcp")
        config = PersonaCoreConfig()
        assert config.mcp_servers_parsed == {"a": "https://a.example/mcp"}

    def test_parses_multiple_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "PERSONA_MCP_SERVERS",
            "a=https://a.example/mcp,b=https://b.example/mcp,c=http://localhost:8080/mcp",
        )
        config = PersonaCoreConfig()
        parsed = config.mcp_servers_parsed
        assert len(parsed) == 3
        assert parsed["a"] == "https://a.example/mcp"
        assert parsed["c"] == "http://localhost:8080/mcp"

    def test_tolerates_whitespace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "PERSONA_MCP_SERVERS",
            " a = https://a.example/mcp , b=https://b.example/mcp ",
        )
        config = PersonaCoreConfig()
        assert config.mcp_servers_parsed == {
            "a": "https://a.example/mcp",
            "b": "https://b.example/mcp",
        }

    def test_rejects_missing_equals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_MCP_SERVERS", "justaname")
        with pytest.raises(ValueError, match="missing '='"):
            PersonaCoreConfig()

    def test_rejects_empty_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_MCP_SERVERS", "=https://x")
        with pytest.raises(ValueError, match="empty server name"):
            PersonaCoreConfig()

    def test_rejects_invalid_name_chars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_MCP_SERVERS", "bad name=https://x.com/mcp")
        with pytest.raises(ValueError, match="invalid server name"):
            PersonaCoreConfig()

    def test_rejects_empty_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_MCP_SERVERS", "a=")
        with pytest.raises(ValueError, match="empty URL"):
            PersonaCoreConfig()

    def test_rejects_non_http_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_MCP_SERVERS", "a=ftp://files.example")
        with pytest.raises(ValueError, match="must start with http"):
            PersonaCoreConfig()

    def test_rejects_duplicate_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_MCP_SERVERS", "a=https://a/mcp,a=https://b/mcp")
        with pytest.raises(ValueError, match="duplicate"):
            PersonaCoreConfig()

    def test_url_in_value_with_equals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # URL containing '=' in query string should survive the split.
        monkeypatch.setenv("PERSONA_MCP_SERVERS", "a=https://a/mcp?token=abc=xyz")
        config = PersonaCoreConfig()
        assert config.mcp_servers_parsed["a"] == "https://a/mcp?token=abc=xyz"


# Section: PersonaCoreConfig — other spec-03 fields


class TestPersonaCoreConfigToolFields:
    def test_web_search_provider_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSONA_WEB_SEARCH_PROVIDER", raising=False)
        config = PersonaCoreConfig()
        assert config.web_search_provider == "brave"

    def test_web_search_provider_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_WEB_SEARCH_PROVIDER", "tavily")
        config = PersonaCoreConfig()
        assert config.web_search_provider == "tavily"

    def test_web_search_provider_rejects_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_WEB_SEARCH_PROVIDER", "duckduckgo")
        with pytest.raises(ValueError, match="duckduckgo|brave|tavily|serpapi"):
            PersonaCoreConfig()

    def test_web_search_api_key_is_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERSONA_WEB_SEARCH_API_KEY", "super-secret")
        config = PersonaCoreConfig()
        assert config.web_search_api_key is not None
        assert config.web_search_api_key.get_secret_value() == "super-secret"
        # SecretStr never appears in repr.
        assert "super-secret" not in repr(config)

    def test_tools_sandbox_root_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSONA_TOOLS_SANDBOX_ROOT", raising=False)
        config = PersonaCoreConfig()
        assert config.tools_sandbox_root.as_posix().endswith(".persona_work")


# Section: build_default_toolbox happy path


class TestBuildDefaultToolboxBasics:
    @pytest.mark.asyncio
    async def test_returns_toolbox_with_builtins(self, tmp_path: Path) -> None:
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["web_search", "web_fetch", "file_read", "file_write"])
        toolbox, mcp_clients = await build_default_toolbox(config, persona)
        assert isinstance(toolbox, Toolbox)
        assert mcp_clients == []
        names = toolbox.names()
        assert "web_search" in names
        assert "web_fetch" in names
        assert "file_read" in names
        assert "file_write" in names

    @pytest.mark.asyncio
    async def test_enabled_but_removed_mcp_server_degrades_not_crashes(
        self, tmp_path: Path
    ) -> None:
        # N2-D-4 surface c: a persona that still enables a now-removed catalog server
        # (``mcp:<name>`` with no configured/live source) must NOT crash toolbox
        # construction. The allow-list entry simply resolves to no advertised tool —
        # graceful degrade, never a hard error.
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["mcp:ghost-server", "file_read"])
        toolbox, mcp_clients = await build_default_toolbox(config, persona)
        names = toolbox.names()
        assert "file_read" in names  # the real allowed tool still resolves
        assert not any(n.startswith("mcp:ghost-server") for n in names)  # the ghost is absent
        assert mcp_clients == []  # no server was configured for the ghost; nothing connected

    @pytest.mark.asyncio
    async def test_persona_allow_list_filters_specs(self, tmp_path: Path) -> None:
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        # Persona only declares file_read.
        persona = _persona(tools=["file_read"])
        toolbox, _ = await build_default_toolbox(config, persona)
        assert toolbox.names() == ["file_read"]

    @pytest.mark.asyncio
    async def test_empty_allow_list_means_no_tools(self, tmp_path: Path) -> None:
        # When persona.tools is empty, allow_list passes through as None
        # which is permissive in development. The factory respects the
        # spec sketch: if there are no tools declared, none are exposed.
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=[])
        toolbox, _ = await build_default_toolbox(config, persona)
        # `None` allow-list is permissive — all built-ins are advertised.
        # If the user wanted to enforce "no tools" they must pass [] explicitly
        # through Toolbox; this factory leaves the development convenience in place.
        # Document: production callers should pass non-empty persona.tools.
        names = toolbox.names()
        # The spec-03 originals plus the spec-26 additions are all advertised.
        assert {"web_search", "web_fetch", "file_read", "file_write"}.issubset(names)
        assert "calculator" in names  # spec 26 T01
        assert "mcp_search" in names  # spec N4 — self-extension discovery tool

    @pytest.mark.asyncio
    async def test_returned_tools_satisfy_async_tool(self, tmp_path: Path) -> None:
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["web_search", "file_write"])
        toolbox, _ = await build_default_toolbox(config, persona)
        # Specs land for the allowed names.
        spec_names = [s.name for s in toolbox.get_specs()]
        assert set(spec_names) == {"web_search", "file_write"}


# Section: build_default_toolbox with MCP


@asynccontextmanager
async def _fake_transport(_url: str) -> Any:  # noqa: ANN401
    yield (MagicMock(name="read"), MagicMock(name="write"), MagicMock(name="sid"))


@asynccontextmanager
async def _fake_session(_r: Any, _w: Any) -> Any:  # noqa: ANN401
    yield SimpleNamespace(
        initialize=AsyncMock(),
        list_tools=AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search",
                        description="MCP search",
                        inputSchema={"type": "object"},
                    )
                ]
            )
        ),
        call_tool=AsyncMock(),
    )


class TestBuildDefaultToolboxWithMCP:
    @pytest.mark.asyncio
    async def test_includes_mcp_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import mcp
        import mcp.client.streamable_http as shttp

        monkeypatch.setattr(shttp, "streamablehttp_client", _fake_transport)
        monkeypatch.setattr(mcp, "ClientSession", _fake_session)

        config = PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            mcp_servers="legal=https://legal.example/mcp",
        )
        persona = _persona(tools=["file_write", "mcp:legal:search"])

        audit = MemoryToolAuditLogger()
        toolbox, clients = await build_default_toolbox(config, persona, tool_audit_logger=audit)

        assert len(clients) == 1
        assert clients[0].is_connected
        names = toolbox.names()
        assert "file_write" in names
        assert "mcp:legal:search" in names

        # Connect audit emitted.
        connect_events = [e for e in audit.events if e.action == "connect"]
        assert len(connect_events) == 1

    @pytest.mark.asyncio
    async def test_extra_mcp_clients_tools_are_auto_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Spec 30 (D-30-4/6): a bring-your-own client's tools are added AND
        # auto-allowed even though the persona's YAML tools never name them
        # (the assignment is the authorization). Fakes accept **kwargs so the
        # SSRF-pinned factory / headers args are tolerated.
        import mcp
        import mcp.client.streamable_http as shttp
        from persona.tools.mcp.client import MCPClient

        @asynccontextmanager
        async def _fake_transport_kw(_url: str, **_kw: Any) -> Any:  # noqa: ANN401
            yield (MagicMock(), MagicMock(), MagicMock())

        monkeypatch.setattr(shttp, "streamablehttp_client", _fake_transport_kw)
        monkeypatch.setattr(mcp, "ClientSession", _fake_session)

        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        # The persona declares only file_read — NOT the BYO server's tools.
        persona = _persona(tools=["file_read"])
        byo = MCPClient(server_name="byo", server_url="https://byo.example/mcp")

        toolbox, clients = await build_default_toolbox(config, persona, extra_mcp_clients=[byo])

        names = toolbox.names()
        assert "file_read" in names
        # The BYO tool is allowed despite not being in persona.tools.
        assert "mcp:byo:search" in names
        assert byo in clients

        for c in clients:
            await c.disconnect()

    @pytest.mark.asyncio
    async def test_use_skill_extra_tool_is_auto_allowed(self, tmp_path: Path) -> None:
        # Regression: a persona that declares an explicit ``tools`` allow-list
        # (which never names ``use_skill`` — it is a composition-root meta-tool,
        # not a persona-declared capability) must STILL advertise the
        # ``use_skill`` tool the runtime/API injects via ``extra_tools`` when the
        # persona has scanned skills. Before the fix the allow-list filtered
        # ``use_skill`` out, so the model never saw it and called the skill name
        # directly → ToolNotAllowedError ("document_generation not available").
        from persona.tools.protocol import tool

        @tool(name="use_skill", description="Activate a skill by name.")
        async def _fake_use_skill(*, skill_name: str) -> str:  # noqa: ARG001
            return "ok"

        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        # The persona declares a normal allow-list — NOT use_skill.
        persona = _persona(tools=["code_execution", "web_search"])
        toolbox, _ = await build_default_toolbox(config, persona, extra_tools=[_fake_use_skill])
        names = toolbox.names()
        assert "use_skill" in names, "use_skill must be advertised even with an explicit allow-list"
        assert toolbox.is_allowed("use_skill")

    @pytest.mark.asyncio
    async def test_schedule_introspect_extra_tool_is_auto_allowed(self, tmp_path: Path) -> None:
        # R9-075: ``schedule_introspect`` is how a persona knows what it has committed to,
        # not a capability anyone opts into — no persona's allow-list names it (it is
        # deliberately absent from TOOL_CATALOG). Registering it without auto-allowing it
        # would change nothing: EVERY persona has a non-empty allow-list (the
        # ensure_default_capabilities floor), so the tool would be composed and then
        # filtered straight back out before the model ever saw it.
        from persona.tools.builtin.schedule_introspection import (
            make_schedule_introspection_tool,
        )

        def _reader_provider() -> None:
            return None

        schedule_tool = make_schedule_introspection_tool(
            reader_provider=_reader_provider, persona_id="astrid"
        )

        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["file_read", "code_execution", "web_search"])
        toolbox, _ = await build_default_toolbox(config, persona, extra_tools=[schedule_tool])

        assert "schedule_introspect" in toolbox.names(), (
            "schedule_introspect must be advertised even with an explicit allow-list"
        )
        assert toolbox.is_allowed("schedule_introspect")

    @pytest.mark.asyncio
    async def test_unreachable_mcp_server_graceful(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Make the transport raise; factory should NOT propagate.
        @asynccontextmanager
        async def broken_transport(_url: str) -> Any:  # noqa: ANN401
            raise ConnectionError("DNS failed")
            yield  # pragma: no cover

        import mcp.client.streamable_http as shttp

        monkeypatch.setattr(shttp, "streamablehttp_client", broken_transport)

        config = PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            mcp_servers="dead=https://nowhere/mcp",
        )
        persona = _persona(tools=["file_read"])

        audit = MemoryToolAuditLogger()
        toolbox, clients = await build_default_toolbox(config, persona, tool_audit_logger=audit)

        # Still got a toolbox; built-ins still work.
        assert isinstance(toolbox, Toolbox)
        # MCP client returned but not connected.
        assert len(clients) == 1
        assert not clients[0].is_connected
        # server_unavailable audited.
        unavail = [e for e in audit.events if e.action == "server_unavailable"]
        assert len(unavail) == 1


# Section: Spec N1 — the Docker MCP Gateway as a 4th MCP source (D-N1-1/2/5/6)


@asynccontextmanager
async def _fake_transport_kw(_url: str, **_kw: Any) -> Any:  # noqa: ANN401
    # Accepts **kwargs so the gateway's headers (bearer) arg is tolerated.
    yield (MagicMock(), MagicMock(), MagicMock())


def _patch_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp
    import mcp.client.streamable_http as shttp

    monkeypatch.setattr(shttp, "streamablehttp_client", _fake_transport_kw)
    monkeypatch.setattr(mcp, "ClientSession", _fake_session)


def _gateway(clients: list[Any]) -> Any:  # noqa: ANN401
    return next((c for c in clients if c.server_name == "docker"), None)


class TestBuildDefaultToolboxWithGateway:
    @pytest.mark.asyncio
    async def test_opted_in_persona_sees_gateway_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D-N1-6: a persona opts in by naming the gateway tool in its allow-list.
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            docker_mcp_gateway_url="http://127.0.0.1:8811/mcp",
        )
        persona = _persona(tools=["mcp:docker:search"])
        toolbox, clients = await build_default_toolbox(config, persona)

        assert "mcp:docker:search" in toolbox.names()  # the opted-in gateway tool
        gw = _gateway(clients)
        assert gw is not None
        assert gw.is_connected

    @pytest.mark.asyncio
    async def test_un_opted_persona_does_not_see_gateway_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The load-bearing D-N1-6 guarantee: enable-once-in-Docker is NOT auto-grant.
        # A persona whose allow-list names no gateway tool sees none — and the gateway
        # is not even connected for it (lazy, no waste).
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            docker_mcp_gateway_url="http://127.0.0.1:8811/mcp",
        )
        persona = _persona(tools=["file_write"])  # explicit allow-list, NO gateway tool
        toolbox, clients = await build_default_toolbox(config, persona)

        assert "mcp:docker:search" not in toolbox.names()
        assert _gateway(clients) is None  # not auto-connected for an un-opted persona

    @pytest.mark.asyncio
    async def test_no_gateway_url_means_no_gateway_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)  # no gateway URL
        persona = _persona(tools=["mcp:docker:search"])
        _toolbox, clients = await build_default_toolbox(config, persona)
        assert _gateway(clients) is None

    @pytest.mark.asyncio
    async def test_gateway_client_is_operator_trust_with_bearer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D-N1-2: operator-trust (enforce_ssrf=False, like PERSONA_MCP_SERVERS, NOT the
        # SSRF-pinned BYO path). D-N1-5: the bearer rides the header path; it is a
        # SecretStr (never logged) and never appears in an audit line.
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            docker_mcp_gateway_url="http://gateway.internal:8811/mcp",
            docker_mcp_gateway_token="gw-secret-token",
        )
        persona = _persona(tools=["mcp:docker:search"])
        audit = MemoryToolAuditLogger()
        _toolbox, clients = await build_default_toolbox(config, persona, tool_audit_logger=audit)

        gw = _gateway(clients)
        assert gw is not None
        assert gw._enforce_ssrf is False  # operator-trust, not SSRF-pinned
        # streaming /mcp URL passed through verbatim (the transport trap — D-N1-1).
        assert gw._server_url == "http://gateway.internal:8811/mcp"
        # bearer rides the header path only.
        assert gw._headers == {"Authorization": "Bearer gw-secret-token"}
        # the token is a SecretStr → never leaked by config repr (D-N1-5).
        assert "gw-secret-token" not in repr(config)
        # the secret never appears in any audit line.
        for event in audit.events:
            assert "gw-secret-token" not in str(event.metadata)


# Section: Spec 26 — built-in tools are wired into build_default_toolbox
# (AC #2 — no Spec 15 §2.9 wiring gap: every new factory has an integration
# test proving the composed Toolbox advertises it when allow-listed).

# Extended per task as Cluster A/B tools land.
_SPEC26_BUILTIN_NAMES = [
    "calculator",
    "datetime",
    "regex_match",
    "currency_convert",
    "json_query",
    "text_diff",
]


class TestSpec26BuiltinsWired:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", _SPEC26_BUILTIN_NAMES)
    async def test_builtin_registered_and_advertised(self, tool_name: str, tmp_path: Path) -> None:
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=[tool_name])
        toolbox, _ = await build_default_toolbox(config, persona)
        # Registered AND advertised to the model when the persona allows it.
        assert tool_name in toolbox.names()
        assert tool_name in [s.name for s in toolbox.get_specs()]


class TestMcpSearchCatalogWiring:
    """Spec N4 B2-②: build_default_toolbox threads a catalog into mcp_search (the
    search-boundary mirror — the api injects the edition-vetted catalog here)."""

    @pytest.mark.asyncio
    async def test_mcp_search_catalog_param_filters_what_mcp_search_surfaces(
        self, tmp_path: Path
    ) -> None:
        from persona.schema.tools import ToolCall
        from persona.tools.mcp.catalog import MCPCatalog, MCPServerCatalogEntry

        vetted = MCPCatalog(
            servers={
                "notion-remote": MCPServerCatalogEntry(
                    name="notion-remote",
                    description="Hosted Notion MCP.",
                    kind="external",
                    risk="low",
                    server_type="remote",
                    remote_url="https://mcp.notion.com/mcp",
                    keywords=("notion",),
                )
            }
        )
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["mcp_search"])
        toolbox, _ = await build_default_toolbox(config, persona, mcp_search_catalog=vetted)
        result = await toolbox.dispatch(ToolCall(name="mcp_search", args={"query": "notion"}))
        assert result.data is not None
        # mcp_search searched the INJECTED vetted catalog, not the default mirror.
        assert [r["name"] for r in result.data["results"]] == ["notion-remote"]


# Section: Spec N7 (D-N7-1) — server-level grants expand to real tool names


class TestServerGrantExpansion:
    """The apps toggle writes the bare ``mcp:<name>`` server grant (N3); composition
    expands it into that server's REAL ``mcp:<name>:<tool>`` names at toolbox build —
    the ``byo_allow`` mechanism generalized to the builtin-launcher / env-configured /
    gateway sources. ``is_allowed`` stays literal exact-match (no prefix magic in the
    hot path); the expansion is fail-closed by construction (a server that did not
    connect THIS build grants nothing)."""

    @pytest.mark.asyncio
    async def test_bare_grant_expands_extra_mcp_server_tools(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The audit's exact bug (N7 §1.1): the builtin launcher hands its spawned
        # servers over as ``extra_mcp_servers``; a persona enabled via the apps
        # toggle (bare ``mcp:<name>``) must actually SEE that server's tools.
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["mcp:launcher"])
        toolbox, _clients = await build_default_toolbox(
            config, persona, extra_mcp_servers={"launcher": "http://127.0.0.1:9/mcp"}
        )
        names = toolbox.names()
        assert "mcp:launcher:search" in names
        assert toolbox.is_allowed("mcp:launcher:search")
        assert "mcp:launcher:search" in [s.name for s in toolbox.get_specs()]

    @pytest.mark.asyncio
    async def test_bare_grant_expands_env_configured_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            mcp_servers="envsrv=https://envsrv.example/mcp",
        )
        persona = _persona(tools=["mcp:envsrv"])
        toolbox, _clients = await build_default_toolbox(config, persona)
        assert "mcp:envsrv:search" in toolbox.names()

    @pytest.mark.asyncio
    async def test_bare_gateway_grant_connects_and_expands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Companion gate bug (N7 §1.3): a persona holding ONLY the bare
        # ``mcp:docker`` grant never even connected the gateway —
        # ``references_gateway`` matched the 3-segment prefix only.
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(
            tools_sandbox_root=tmp_path,
            docker_mcp_gateway_url="http://127.0.0.1:8811/mcp",
        )
        persona = _persona(tools=["mcp:docker"])
        toolbox, clients = await build_default_toolbox(config, persona)
        gw = _gateway(clients)
        assert gw is not None, "a bare mcp:docker grant must connect the gateway"
        assert gw.is_connected
        assert "mcp:docker:search" in toolbox.names()

    @pytest.mark.asyncio
    async def test_grant_never_bleeds_across_servers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail-closed pin: granting server ``alpha`` admits NOTHING of ``beta``,
        # even though both are connected and discovered in the same build.
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["mcp:alpha"])
        toolbox, _clients = await build_default_toolbox(
            config,
            persona,
            extra_mcp_servers={
                "alpha": "http://127.0.0.1:9/mcp",
                "beta": "http://127.0.0.1:10/mcp",
            },
        )
        names = toolbox.names()
        assert "mcp:alpha:search" in names
        assert "mcp:beta:search" not in names
        assert not toolbox.is_allowed("mcp:beta:search")

    @pytest.mark.asyncio
    async def test_unknown_grant_admits_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A grant for a server that is not configured anywhere admits nothing —
        # and toolbox construction stays graceful (the N2-D-4 ghost shape).
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["mcp:nosuch", "file_read"])
        toolbox, _clients = await build_default_toolbox(
            config, persona, extra_mcp_servers={"alpha": "http://127.0.0.1:9/mcp"}
        )
        names = toolbox.names()
        assert "file_read" in names
        assert not any(n.startswith("mcp:") for n in names)

    @pytest.mark.asyncio
    async def test_failed_connect_grants_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fail-closed: the grant is present but the server's transport dies at
        # connect — strict=False degrades gracefully and the grant admits nothing.
        import mcp
        import mcp.client.streamable_http as shttp

        @asynccontextmanager
        async def broken_transport(_url: str, **_kw: Any) -> Any:  # noqa: ANN401
            msg = "connection refused"
            raise RuntimeError(msg)
            yield  # pragma: no cover — unreachable; keeps the contextmanager shape

        monkeypatch.setattr(shttp, "streamablehttp_client", broken_transport)
        monkeypatch.setattr(mcp, "ClientSession", _fake_session)
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["mcp:dead", "file_read"])
        toolbox, _clients = await build_default_toolbox(
            config, persona, extra_mcp_servers={"dead": "http://127.0.0.1:9/mcp"}
        )
        names = toolbox.names()
        assert "file_read" in names
        assert not any(n.startswith("mcp:dead") for n in names)

    @pytest.mark.asyncio
    async def test_grant_and_tool_form_coexist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A persona may carry both the bare grant AND an explicit 3-segment
        # declaration for the same server — advertised once, no duplicate error.
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["mcp:launcher", "mcp:launcher:search"])
        toolbox, _clients = await build_default_toolbox(
            config, persona, extra_mcp_servers={"launcher": "http://127.0.0.1:9/mcp"}
        )
        assert toolbox.names().count("mcp:launcher:search") == 1

    @pytest.mark.asyncio
    async def test_bare_grant_entry_is_never_advertised_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The bare entry is the DOCUMENTED grant form in the allow-list — but no
        # tool registers under it, so it is never advertised to the model.
        _patch_mcp(monkeypatch)
        config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
        persona = _persona(tools=["mcp:launcher", "file_read"])
        toolbox, _clients = await build_default_toolbox(
            config, persona, extra_mcp_servers={"launcher": "http://127.0.0.1:9/mcp"}
        )
        assert "mcp:launcher" not in toolbox.names()
