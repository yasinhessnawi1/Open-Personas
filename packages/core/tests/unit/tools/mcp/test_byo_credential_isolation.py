"""Spec N4 B1 — per-user / adopted-app credential isolation proof (criterion 3).

N4 adopts a remote app by REUSING the Spec-30 bring-your-own path (N4-D-1): the per-user
credential is Fernet-stored, transient-decrypted at connect, and injected as a
**transport-layer** ``Authorization`` header bound to the ``MCPClient`` (the api's
``runtime_factory._build_byo_mcp_clients`` builds ``MCPClient(headers={"Authorization":
f"Bearer {credential}"})``; ``MCPClient.connect`` forwards it to ``streamablehttp_client``,
client.py:145-151).

This is the per-user analog of N1's operator-bearer T5 proof
(:mod:`test_gateway_credential_isolation`). It proves — ADVERSARIALLY — that the per-user
credential reaches the transport (so its absence elsewhere is *meaningful*, not vacuous)
yet appears in NONE of the model-facing surfaces: tool specs, tool-call args, tool result,
audit. The secret never round-trips a model turn. The store→encrypt→decrypt half is the
reused Spec-30 chain (``mcp/crypto.py`` + ``mcp/store.py``, tested there); the exhaustive
five-channel sweep + prompt-injection-exfil attempt is Group E.
"""

# ruff: noqa: ANN401 — the MCP SDK fakes are intentionally Any-typed
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from persona.config import PersonaCoreConfig
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolCall
from persona.tools import build_default_toolbox
from persona.tools.audit import MemoryToolAuditLogger
from persona.tools.mcp.client import MCPClient

if TYPE_CHECKING:
    from pathlib import Path

# A per-user credential — the value a user supplies at adoption setup, the way an
# adopted app's secret lands in the encrypted store and is injected at connect.
_SECRET = "byo-per-user-token-do-not-leak"  # noqa: S105 — test sentinel, not a real credential

#: Headers the (faked) transport actually received — proving the credential reaches the wire.
_captured_headers: list[dict[str, str] | None] = []


@asynccontextmanager
async def _capturing_transport(_url: str, **kwargs: Any) -> Any:
    _captured_headers.append(kwargs.get("headers"))
    yield (object(), object(), object())


@asynccontextmanager
async def _fake_session(_r: Any, _w: Any) -> Any:
    yield SimpleNamespace(
        initialize=AsyncMock(),
        list_tools=AsyncMock(
            return_value=SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search",
                        description="Search the adopted app.",
                        inputSchema={"type": "object"},
                    )
                ]
            )
        ),
        call_tool=AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(text="result: found 3 items")],
                structuredContent=None,
                isError=False,
            )
        ),
    )


def _persona() -> Persona:
    # Declares only a built-in; the adopted (BYO) server's tools are auto-allowed by
    # assignment (D-30-6), so the YAML never names — and never sees — the credential.
    return Persona(
        persona_id="n4-iso-test",
        identity=PersonaIdentity(
            name="T", role="R", background="A persona for the adopted-credential isolation proof."
        ),
        tools=["file_read"],
    )


@pytest.fixture
def patch_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    import mcp
    import mcp.client.streamable_http as shttp

    _captured_headers.clear()
    monkeypatch.setattr(shttp, "streamablehttp_client", _capturing_transport)
    monkeypatch.setattr(mcp, "ClientSession", _fake_session)


@pytest.mark.asyncio
@pytest.mark.usefixtures("patch_mcp")
async def test_adopted_credential_reaches_transport_but_no_model_facing_surface(
    tmp_path: Path,
) -> None:
    config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
    audit = MemoryToolAuditLogger()
    # The post-decrypt injection shape: the per-user credential as a transport header,
    # exactly as ``_build_byo_mcp_clients`` constructs it after transient-decrypt.
    adopted = MCPClient(
        server_name="adopted",
        server_url="https://adopted.example/mcp",
        headers={"Authorization": f"Bearer {_SECRET}"},
    )
    toolbox, clients = await build_default_toolbox(
        config, _persona(), extra_mcp_clients=[adopted], tool_audit_logger=audit
    )

    # (1) the credential DID reach the transport — so its absence elsewhere is meaningful.
    assert _captured_headers, "the adopted server's transport was never opened"
    assert any(
        h is not None and h.get("Authorization") == f"Bearer {_SECRET}" for h in _captured_headers
    ), "the per-user credential never reached the transport header"

    # (2) assemble EVERY model-facing surface the adopted tool contributes.
    surfaces: list[str] = []
    surfaces += [s.model_dump_json() for s in toolbox.get_specs()]  # (a) tool specs
    call = ToolCall(name="mcp:adopted:search", args={"q": "hello"})  # (b) tool-call args
    result = await toolbox.dispatch(call)  # (c) the tool result
    surfaces.append(call.model_dump_json())
    surfaces.append(result.model_dump_json())
    surfaces += [  # (d) the audit log
        json.dumps({"action": e.action, "tool": e.tool_name, "meta": e.metadata})
        for e in audit.events
    ]

    # the dispatch really ran (the result surface is real, not skipped)
    assert "found 3 items" in result.content
    assert "mcp:adopted:search" in toolbox.names()

    # (3) the adversarial grep: the per-user credential appears in NONE of the surfaces.
    leaks = [s for s in surfaces if _SECRET in s]
    assert leaks == [], f"adopted credential leaked into {len(leaks)} model-facing surface(s)"

    for c in clients:
        await c.disconnect()


@pytest.mark.asyncio
@pytest.mark.usefixtures("patch_mcp")
async def test_credential_is_not_in_the_advertised_tool_spec(tmp_path: Path) -> None:
    # The adopted tool IS advertised to the model (by name) — but the spec the model sees
    # carries the tool's schema, never the connection credential.
    config = PersonaCoreConfig(tools_sandbox_root=tmp_path)
    adopted = MCPClient(
        server_name="adopted",
        server_url="https://adopted.example/mcp",
        headers={"Authorization": f"Bearer {_SECRET}"},
    )
    toolbox, clients = await build_default_toolbox(config, _persona(), extra_mcp_clients=[adopted])

    spec_names = [s.name for s in toolbox.get_specs()]
    # Advertised by name — its WIRE name, since a colon in a function name 400s the whole
    # request at every provider. Resolved back through the toolbox so this asserts the tool
    # is offered, not how the name happens to be spelled on the wire.
    assert "mcp:adopted:search" in [toolbox.real_name(n) for n in spec_names]
    assert all(_SECRET not in s.model_dump_json() for s in toolbox.get_specs())  # never the secret

    for c in clients:
        await c.disconnect()
