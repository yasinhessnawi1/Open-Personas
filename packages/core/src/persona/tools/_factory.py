"""``build_default_toolbox`` — compose a Toolbox from config + Persona (T12).

Wires the four built-in tools and (asynchronously) loads any MCP servers
declared in :class:`PersonaCoreConfig.mcp_servers`. The persona's
``tools`` allow-list filters which tools the Toolbox advertises.

Graceful degradation: MCP servers are connected with ``strict=False``
per D-03-20 — unreachable servers log a warning and audit a
``server_unavailable`` event, but the toolbox still builds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.tools.builtin.calculator import make_calculator_tool
from persona.tools.builtin.currency_convert import make_currency_convert_tool
from persona.tools.builtin.datetime import make_datetime_tool
from persona.tools.builtin.file_read import make_file_read_tool
from persona.tools.builtin.file_write import make_file_write_tool
from persona.tools.builtin.json_query import make_json_query_tool
from persona.tools.builtin.mcp_search import make_mcp_search_tool
from persona.tools.builtin.regex_match import make_regex_match_tool
from persona.tools.builtin.schedule_introspection import SCHEDULE_INTROSPECT_TOOL_NAME
from persona.tools.builtin.text_diff import make_text_diff_tool
from persona.tools.builtin.web_fetch import make_web_fetch_tool
from persona.tools.builtin.web_search import make_web_search_tool
from persona.tools.mcp.client import MCPClient, load_mcp_clients
from persona.tools.mcp.naming import referenced_server_name, server_grant_name
from persona.tools.toolbox import Toolbox

if TYPE_CHECKING:
    from persona.config import PersonaCoreConfig
    from persona.schema.persona import Persona
    from persona.tools._sandbox import SandboxRootProvider
    from persona.tools.audit import ToolAuditLogger
    from persona.tools.mcp.catalog import MCPCatalog
    from persona.tools.protocol import AsyncTool
    from persona.tools.workspace_persister import WorkspacePersister

__all__ = ["SELF_KNOWLEDGE_TOOLS", "build_default_toolbox"]

_logger = get_logger("tools.factory")

#: Composition-injected tools whose PRESENCE in ``extra_tools`` IS the authorization —
#: they are auto-allowed rather than gated on the persona's ``tools`` list.
#:
#: These are not capabilities a persona opts into; they are how a persona knows things
#: about itself, and they are never named in a YAML allow-list (they are absent from
#: :data:`persona.tools.catalog.TOOL_CATALOG` for exactly that reason). Without the
#: auto-allow, ``build_default_toolbox`` REGISTERS them and then filters them straight
#: back out for every persona with a non-empty allow-list — which is every persona, since
#: :func:`persona.schema.defaults.ensure_default_capabilities` guarantees a floor of three.
#: The tool would exist, be composed, be logged in ``extra_tool_count``, and still never
#: reach the model.
#:
#: * ``use_skill`` — the skill-dispatch meta-tool (D-04-10). The allow-list enumerates
#:   capabilities, not the dispatch mechanism; without this the model calls the skill name
#:   directly and gets ``ToolNotAllowedError`` ("document_generation is not available").
#: * ``schedule_introspect`` — the read-only calendar window (R9-075). A persona could
#:   create schedules but had no way to read them back, so "what's on my calendar?" was
#:   answered from imagination.
SELF_KNOWLEDGE_TOOLS: frozenset[str] = frozenset({"use_skill", SCHEDULE_INTROSPECT_TOOL_NAME})

#: The server name the Docker MCP Gateway registers under (Spec N1, D-N1-6); its
#: aggregated tools are prefixed ``mcp:docker:<tool>``.
_GATEWAY_SERVER_NAME = "docker"


def _build_gateway_client(
    config: PersonaCoreConfig,
    persona: Persona,
    audit_logger: ToolAuditLogger | None,
) -> MCPClient | None:
    """Build the Docker MCP Gateway client, or ``None`` (Spec N1, D-N1-1/2/5/6).

    Returns ``None`` when no gateway URL is configured (fail-soft) OR the persona's
    explicit allow-list references no gateway tool — neither a 3-segment
    ``mcp:docker:<tool>`` entry nor the bare ``mcp:docker`` server grant (Spec N7,
    D-N7-1: the form the apps toggle writes) — so a gateway a persona can't use
    is never connected (lazy, mirroring the D-27-3 built-in lazy-spawn discipline). A
    persona with NO declared tools is the all-allowed dev/CLI path, so it does connect.

    The client is **connect-only, operator-trust**: ``enforce_ssrf=False`` (the gateway
    URL is operator deployment config, same posture as the ``PERSONA_MCP_SERVERS``
    channel, D-N1-2), bearer header only when a token is set (D-N1-5 — a ``SecretStr``,
    never logged). Tools are prefixed ``mcp:docker:`` via ``server_name``.

    Crucially, the returned client's tools are added to the GATED ``mcp_tools`` path by
    the caller (filtered by the persona's ``tools`` allow-list), NOT auto-allowed like
    assigned BYO servers — "enable once in Docker, then opt each persona in" (D-N1-6).
    """
    url = config.docker_mcp_gateway_url
    if not url:
        return None
    prefix = f"mcp:{_GATEWAY_SERVER_NAME}:"
    references_gateway = (not persona.tools) or any(
        t.startswith(prefix) or server_grant_name(t) == _GATEWAY_SERVER_NAME for t in persona.tools
    )
    if not references_gateway:
        return None
    token = config.docker_mcp_gateway_token
    headers = {"Authorization": f"Bearer {token.get_secret_value()}"} if token else None
    return MCPClient(
        server_name=_GATEWAY_SERVER_NAME,
        server_url=url,
        audit_logger=audit_logger,
        persona_id=persona.persona_id,
        enforce_ssrf=False,  # operator deployment config — operator-channel posture (D-N1-2)
        headers=headers,
    )


async def build_default_toolbox(
    config: PersonaCoreConfig,
    persona: Persona,
    *,
    tool_audit_logger: ToolAuditLogger | None = None,
    extra_tools: list[AsyncTool] | None = None,
    workspace_persister: WorkspacePersister | None = None,
    extra_mcp_servers: dict[str, str] | None = None,
    extra_mcp_clients: list[MCPClient] | None = None,
    file_sandbox_root: SandboxRootProvider | None = None,
    mcp_search_catalog: MCPCatalog | None = None,
) -> tuple[Toolbox, list[MCPClient]]:
    """Compose a Toolbox for the given persona.

    Args:
        config: Runtime configuration with `web_search_*`, `tools_sandbox_root`,
            and `mcp_servers` fields populated from env vars (spec-03 D-03-9
            through D-03-23).
        persona: The persona whose `tools` allow-list filters which tools
            the Toolbox advertises. Empty allow-list means the Toolbox
            advertises nothing (still safe to dispatch through; every call
            raises `ToolNotAllowedError`).
        tool_audit_logger: Optional logger for `file_write` + MCP lifecycle
            events (D-03-21).
        extra_tools: Additional tools the composition root supplies — notably the
            `use_skill` tool (D-04-10: NOT auto-registered; the runtime/API
            composes it when the persona has skills). Folded into the Toolbox
            alongside the built-ins + MCP tools, subject to the same allow-list.
        workspace_persister: Optional `WorkspacePersister` injected into
            `file_write` so written files are mirrored to the persona workspace
            and surfaced as `ToolResult.artifacts` (inline file cards). `None`
            (CLI / tests) ⇒ `file_write` produces its pre-persister result shape.
        extra_mcp_servers: Additional ``{name: url}`` MCP servers to connect
            beyond those in ``config.mcp_servers`` (Spec 27 D-27-3). The API
            launcher passes the lazily-spawned built-in MCP server URLs here;
            entries override same-named ``config.mcp_servers`` entries. ``None``
            (CLI / test path) ⇒ only the env-configured servers are connected.
        file_sandbox_root: Optional override for the ``file_read`` / ``file_write``
            sandbox root SOURCE (SECURITY — cross-context isolation). ``None``
            (CLI / test path) ⇒ the file tools use the static
            ``config.tools_sandbox_root`` (process-wide, unscoped — acceptable
            for the single-tenant CLI). The hosted API passes a per-request
            *provider* (``Callable[[], Path | None]``) that returns the current
            request's ``<workspace_root>/<owner_id>/<persona_id>`` root, resolved
            at dispatch time from the sandbox request context — so a single
            cached toolbox stays scoped to the calling owner/persona. A provider
            that returns ``None`` makes the file tools fail closed (deny), never
            falling back to the shared root. See
            :func:`persona.tools._sandbox.resolve_request_sandbox_root`.
        extra_mcp_clients: Spec 30 (D-30-4/6) — pre-built bring-your-own MCP
            clients (constructed with ``enforce_ssrf=True`` + any auth headers by
            the API factory, which holds the decryption key). They are connected
            here (``strict=False``, graceful) and their tools are added to the
            toolbox AND auto-allowed: the persona↔server *assignment* is the
            authorization (D-30-6), so BYO tool names are admitted regardless of
            the YAML ``tools`` allow-list (which never names them).

    Returns:
        A tuple ``(toolbox, mcp_clients)``. The caller is responsible for
        eventually calling ``await client.disconnect()`` on each MCP client
        (typically during shutdown). The clients are returned even when
        their connect failed (graceful degradation) so the caller can
        still disconnect any that succeeded.
    """
    # Built-in tools (always present; the persona's allow-list decides
    # whether they're exposed via get_specs / dispatch).
    api_key = (
        config.web_search_api_key.get_secret_value()
        if config.web_search_api_key is not None
        else None
    )
    builtins: list[AsyncTool] = [
        make_web_search_tool(
            provider_name=config.web_search_provider,
            api_key=api_key,
        ),
        make_web_fetch_tool(),
        make_file_read_tool(sandbox_root=file_sandbox_root or config.tools_sandbox_root),
        make_file_write_tool(
            sandbox_root=file_sandbox_root or config.tools_sandbox_root,
            audit_logger=tool_audit_logger,
            persona_id=persona.persona_id,
            persister=workspace_persister,
        ),
        # Spec 26 — general-utility built-ins (deny-by-default; the persona's
        # allow-list still gates whether each is advertised).
        make_calculator_tool(),
        make_datetime_tool(),
        make_regex_match_tool(),
        make_json_query_tool(),
        make_text_diff_tool(),
        # Spec N4 — self-extension: discover apps for an in-role capability gap.
        # Searches the mirrored catalog (display metadata only; never a secret). The api
        # passes the edition-vetted catalog (N4-D-6 search-boundary mirror); None →
        # default mirror (CLI / community-equivalent).
        make_mcp_search_tool(catalog=mcp_search_catalog),
        make_currency_convert_tool(
            provider_name=config.currency_provider,
            api_key=(
                config.currency_api_key.get_secret_value()
                if config.currency_api_key is not None
                else None
            ),
        ),
    ]

    # MCP-discovered tools. Graceful degradation per D-03-20. Built-in MCP
    # servers (Spec 27) arrive via ``extra_mcp_servers`` and override same-named
    # env-configured entries.
    mcp_clients: list[MCPClient] = []
    mcp_tools: list[AsyncTool] = []
    parsed_servers = {**config.mcp_servers_parsed, **(extra_mcp_servers or {})}
    if parsed_servers:
        mcp_clients = await load_mcp_clients(
            parsed_servers,
            audit_logger=tool_audit_logger,
            persona_id=persona.persona_id,
            strict=False,
        )
        for c in mcp_clients:
            mcp_tools.extend(c.get_tools())

    # Spec N1 (D-N1-1/6) — the Docker MCP Gateway as a 4th MCP source. Connect-only,
    # streamable-HTTP, operator-trust (enforce_ssrf=False, D-N1-2). Its aggregated tools
    # are prefixed ``mcp:docker:`` and ride the GATED ``mcp_tools`` path — the persona's
    # ``tools`` allow-list is the gate, so it is "enable once in Docker, then opt each
    # persona in," never auto-grant (D-N1-6). Graceful: an unreachable gateway (strict=
    # False) simply contributes no tools, exactly like any other MCP source.
    gateway_client = _build_gateway_client(config, persona, tool_audit_logger)
    if gateway_client is not None:
        await gateway_client.connect(strict=False)
        mcp_tools.extend(gateway_client.get_tools())
        mcp_clients.append(gateway_client)

    # Spec N7 (D-N7-1) — server-level grant expansion. The apps UX toggle writes the
    # bare server-grant form ``mcp:<name>`` (N3); expand each grant into that server's
    # REAL discovered ``mcp:<name>:<tool>`` names so the literal allow-list admits
    # them — the ``byo_allow`` mechanism below, generalized to all three shared-list
    # MCP sources (built-in launcher via ``extra_mcp_servers``, env-configured
    # ``PERSONA_MCP_SERVERS``, and the gateway). Fail-closed by construction: only
    # tools whose server actually connected THIS build are in ``mcp_tools``, so a
    # dead / unknown / unavailable server grants nothing — and ``is_allowed`` stays
    # literal exact-match (no prefix magic in the hot path). Per-tool deny within an
    # enabled server is v2 (D-N7-1).
    server_grants = {g for t in persona.tools if (g := server_grant_name(t)) is not None}
    grant_allow = [
        t.name
        for t in mcp_tools
        if (ref := referenced_server_name(t.name)) is not None and ref in server_grants
    ]

    # Spec 30 (D-30-4/6) — bring-your-own MCP clients (SSRF-pinned, pre-built by
    # the API factory). Connect gracefully; their tool names are auto-allowed
    # because the assignment is the authorization (the YAML allow-list never
    # names them). A server that fails to connect simply contributes no tools.
    byo_tools: list[AsyncTool] = []
    byo_allow: list[str] = []
    for c in extra_mcp_clients or []:
        await c.connect(strict=False)
        client_tools = c.get_tools()
        byo_tools.extend(client_tools)
        byo_allow.extend(t.name for t in client_tools)
        mcp_clients.append(c)

    all_tools: list[AsyncTool] = [*builtins, *mcp_tools, *byo_tools, *(extra_tools or [])]

    _logger.info(
        "build_default_toolbox composed",
        persona_id=persona.persona_id or "<unknown>",
        builtin_count=len(builtins),
        mcp_tool_count=len(mcp_tools),
        byo_mcp_tool_count=len(byo_tools),
        extra_tool_count=len(extra_tools or []),
        allow_list_size=len(persona.tools),
    )

    # Composition-injected tools whose PRESENCE is the authorization — see
    # ``SELF_KNOWLEDGE_TOOLS`` above. Auto-allowed whenever injected, matching the BYO
    # precedent below.
    injected_allow = [t.name for t in (extra_tools or []) if t.name in SELF_KNOWLEDGE_TOOLS]

    # The allow-list: the persona's declared tools PLUS the server-grant expansion
    # (D-N7-1) PLUS the assigned BYO tool names PLUS the auto-allowed injected tools.
    # The bare ``mcp:<name>`` grant entries stay in the list — they are
    # the documented grant form; no tool registers under a bare name, so they are
    # never advertised. When the persona declares nothing (dev-permissive None
    # path) every tool is allowed anyway (all-allowed), preserving prior behaviour.
    allow_list = (
        [*persona.tools, *grant_allow, *byo_allow, *injected_allow] if persona.tools else None
    )
    toolbox = Toolbox(all_tools, allow_list=allow_list)
    return toolbox, mcp_clients
