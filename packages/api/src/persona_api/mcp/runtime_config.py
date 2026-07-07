"""Env-driven configuration for the per-tenant MCP runtime (Spec N6, T5b, N6-D-3/5).

Read once at process start by the composition root (``app.py``), which builds the
:class:`~persona_api.mcp.fly_runtime.FlyPerTenantMCPRuntime` from it. The ``PERSONA_MCP_RUNTIME_``
prefix is the runtime's own scope (distinct from ``PERSONA_SANDBOX_`` and the API-scope knobs).

``configured`` is the single "is the per-tenant runtime on?" signal the startup guard
(:func:`persona_api.editions.per_tenant_mcp_guard.check_per_tenant_mcp_posture`) consumes: a Fly
app AND a token must both be set. Unset ⇒ the runtime is ``None`` and image-runtime servers are
simply not connected (the not-connected signal, T6) — community / CLI / cloud-without-Fly.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["FlyRuntimeConfig"]


class FlyRuntimeConfig(BaseSettings):
    """The Fly per-tenant MCP runtime knobs (N6-D-1/3)."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore", populate_by_name=True)

    fly_app: str = Field(
        default="",
        validation_alias="PERSONA_MCP_RUNTIME_FLY_APP",
        description="The Fly app the per-tenant Machines live in. Empty ⇒ runtime off.",
    )
    fly_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="FLY_API_TOKEN",
        description="The Fly Machines API token (Bearer). Empty ⇒ runtime off. Never logged.",
    )
    port: int = Field(
        default=8000,
        validation_alias="PERSONA_MCP_RUNTIME_FLY_PORT",
        description="The port the in-Machine stdio→HTTP bridge serves /mcp on.",
    )
    idle_timeout_s: float = Field(
        default=300.0,
        validation_alias="PERSONA_MCP_RUNTIME_IDLE_TIMEOUT_S",
        description="Seconds idle before a per-tenant Machine is reaped (N6-D-3).",
    )
    reap_interval_s: float = Field(
        default=60.0,
        validation_alias="PERSONA_MCP_RUNTIME_REAP_INTERVAL_S",
        description="Background idle-reaper sweep cadence (N6-D-3).",
    )
    max_per_tenant: int = Field(
        default=3,
        validation_alias="PERSONA_MCP_RUNTIME_MAX_PER_TENANT",
        description="Per-tenant concurrent image-runtime cap (N6-D-3; enforced at ASSIGN).",
    )

    @property
    def configured(self) -> bool:
        """True ⇒ the per-tenant runtime is on (a Fly app AND a token are set)."""
        return bool(self.fly_app.strip()) and bool(self.fly_token.get_secret_value().strip())
