"""The per-tenant MCP runtime edition posture (Spec N6, N6-D-5).

The honest edition split for the per-tenant image-MCP runtime, gated off the existing
``PERSONA_EDITION`` flag (Spec 33, D-33-1) — no parallel mechanism, mirroring
:mod:`persona_api.editions.gateway_guard`:

- **community / local** — **byte-unchanged from N1**: the user runs their own Docker +
  gateway (one tenant, they own the trust choice). There is NO per-tenant Fly runtime.
  **No gate.**
- **cloud / hosted** — the per-tenant Fly runtime runs a user's chosen image-MCP server per
  tenant with their secret injected: third-party code executed per-tenant, at a
  per-active-tenant cost. Because that is a deliberate trust + cost decision, the operator
  MUST explicitly acknowledge it via ``PERSONA_ALLOW_PER_TENANT_MCP=1`` — mirroring the
  D-N1-7 cloud-gateway ack and the D-33-4 public-noauth guard. Without the ack + the runtime
  configured (a Fly app set), the API **refuses to start**; with the ack it warns (posture
  recorded) and proceeds.

This is the security-model criterion #6 made concrete: **no arbitrary third-party-container
execution in the hosted product without the ack** — now per-tenant-scoped (the runnable
allow-list, N6-D-4, is the *which images* control; this ack is the *operator asserts the
posture* control).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.logging import get_logger

from persona_api.config import Edition
from persona_api.errors import PerTenantMCPNotAckedError

if TYPE_CHECKING:
    from persona_api.config import APIConfig

__all__ = ["check_per_tenant_mcp_posture"]

_LOG = get_logger("api.editions.per_tenant_mcp_guard")


def check_per_tenant_mcp_posture(config: APIConfig, *, runtime_configured: bool) -> None:
    """Enforce the cloud per-tenant-MCP ack at startup (N6-D-5).

    Args:
        config: The API config (its ``edition`` + ``allow_per_tenant_mcp`` drive the gate).
        runtime_configured: Whether the per-tenant Fly runtime is configured (a Fly app is
            set). The composition root computes this from the runtime env (T5) and injects
            it — the guard itself reads no runtime env, keeping it unit-testable.

    Raises:
        PerTenantMCPNotAckedError: cloud edition + the runtime configured +
            ``PERSONA_ALLOW_PER_TENANT_MCP`` unset.
    """
    if not runtime_configured:
        return  # no per-tenant runtime configured — nothing to gate (fail-soft)
    if config.edition is not Edition.cloud:
        return  # community: N1's local path, byte-unchanged — never gated
    if config.allow_per_tenant_mcp:
        _LOG.warning(
            "cloud per-tenant MCP runtime enabled: runs a user's chosen image-MCP server "
            "per tenant with their secret injected (PERSONA_ALLOW_PER_TENANT_MCP set). "
            "Images are gated by the runnable allow-list (PERSONA_MCP_RUN_VETTED, N6-D-4); "
            "a per-active-tenant cost applies (N6-D-3)."
        )
        return
    raise PerTenantMCPNotAckedError(
        "refusing to start: PERSONA_EDITION=cloud with the per-tenant MCP runtime configured "
        "but PERSONA_ALLOW_PER_TENANT_MCP unset. It runs third-party image-MCP servers "
        "per-tenant with per-user secrets at a per-active-tenant cost; set "
        "PERSONA_ALLOW_PER_TENANT_MCP=1 to acknowledge, or unset the runtime's Fly app.",
        context={"edition": config.edition.value},
    )
