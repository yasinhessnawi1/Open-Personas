"""The not-connected signal for a persona's assigned MCP servers (Spec N6, T6, N6-D-6; R4-C1-21).

Answers "assigned — but is it working?" so the UI can show "assigned" as visibly distinct from
"connected". One RLS-scoped read path (under ``persona_app``, never the reaper's bypass engine)
drives BOTH the API status field and the UI badge; no secret is touched (the instance value is
secretless by construction).

- **image-runtime servers** (catalog ``server_type == "server"``) map through their per-tenant
  runtime instance state (:meth:`MCPRuntimeInstance.to_connection`); an assigned image with no
  instance row yet reports ``not_connected("not_enabled")``;
- **remote / BYO servers** (N4) report through their existing path **unaffected** — they connect
  directly via the N4 client, so the signal marks them ``connected``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_api.mcp import runtime_store
from persona_api.mcp import store as mcp_store
from persona_api.mcp.runtime import MCPServerConnection
from persona_api.services import catalog_service

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

__all__ = ["persona_mcp_connection_status"]


def persona_mcp_connection_status(
    *, rls_engine: Engine, owner_id: str, persona_id: str
) -> list[MCPServerConnection]:
    """Per-assigned-server connection status for a persona (RLS-scoped; N6-D-6).

    Two RLS-scoped reads under ``persona_app`` — the persona's assigned servers and the owner's
    runtime instances — mapped in memory to the T1 vocabulary. Disabled servers are omitted.
    """
    image_names = {
        e.name for e in catalog_service.merged_mcp_catalog() if e.server_type == "server"
    }
    assigned = mcp_store.list_servers_for_persona(rls_engine=rls_engine, persona_id=persona_id)
    instances = runtime_store.list_for_owner(rls_engine=rls_engine, owner_id=owner_id)
    out: list[MCPServerConnection] = []
    for s in assigned:
        if not s.get("enabled"):
            continue
        name = str(s["name"])
        if name in image_names:
            inst = instances.get(str(s["id"]))
            if inst is None:
                # Assigned but the per-tenant runtime has no instance for it (never resolved,
                # or the runtime is unconfigured) → not connected, distinctly from "working".
                out.append(MCPServerConnection.make_not_connected(name, "not_enabled"))
            else:
                out.append(inst.to_connection(name))
        else:
            # Remote / BYO (N4) — connected via the existing client path, unaffected by N6.
            out.append(MCPServerConnection.make_connected(name))
    return out
