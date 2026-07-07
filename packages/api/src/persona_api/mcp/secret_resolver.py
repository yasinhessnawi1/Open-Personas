"""The live per-user secret resolver for the per-tenant MCP runtime (Spec N6, T3, N6-D-2).

N1 reserved the ``GatewaySecretResolver`` Protocol (``persona.tools.mcp.gateway_secrets``)
and left it **implemented never in v1** — a shared connect-only gateway can't vary a
per-user secret (D-N1-5 / N4-D-1a). N6 gives it its real consumer (the one N4-D-1b deferred
to "the per-user-gateway work"): :class:`FernetGatewaySecretResolver` resolves a tenant's
stored secret and the runtime injects it as the Fly Machine's env at spawn (N6-D-2).

**Why this keeps the core N1 marker test green (the N6-D-2 finding):** the resolver lives
HERE, at the api layer (which holds ``MCP_CREDENTIAL_KEY`` + the DB), NOT in core
``persona.tools._factory``. The core connect path still only receives a pre-built
``MCPClient`` with a URL, so ``test_n4_per_user_injection_is_a_reserved_seam_not_wired``
(which asserts the resolver is absent from core ``_factory``) stays green. This module's
own test proves the resolver is now implemented AND called.

**Isolation invariant (acceptance #2/#3), structural not behavioural:** the secret flows
user → Spec-30 Fernet store → :meth:`resolve` (transient in-memory decrypt) → the runtime's
``secret_env`` → the Machine's env → the MCP process. It NEVER enters a returned value, a
model turn, a log line, or an error body. Reuses N4's store + its single key — no second key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.logging import get_logger

from persona_api.mcp import store as mcp_store

if TYPE_CHECKING:
    from persona.tools.mcp.catalog import MCPSecretField, MCPServerCatalogEntry
    from sqlalchemy import Engine

    from persona_api.config import APIConfig
    from persona_api.mcp.runtime import PerTenantMCPRuntime  # noqa: F401 — doc reference

__all__ = ["FernetGatewaySecretResolver", "build_spawn_env"]

_LOG = get_logger("api.mcp.secret_resolver")


class FernetGatewaySecretResolver:
    """Resolve a tenant's stored secret for a server (implements ``GatewaySecretResolver``).

    Structurally satisfies :class:`persona.tools.mcp.gateway_secrets.GatewaySecretResolver`
    (the N1 seam) — the runtime-checkable Protocol only requires the ``resolve`` method.

    Args:
        rls_engine: The per-tenant (``persona_app``, RLS) engine — scopes every read to the
            request's owner via the ``current_user_id`` ContextVar.
        config: The API config (its ``mcp_credential_key`` drives the Spec-30 cipher).
    """

    def __init__(self, *, rls_engine: Engine, config: APIConfig) -> None:
        self._rls = rls_engine
        self._config = config

    def resolve(self, *, owner_id: str, server_name: str, field: MCPSecretField) -> str | None:
        """Return the tenant's stored secret value for ``server_name``, or ``None``.

        ``owner_id`` is accepted for the N1 seam signature but the RLS engine already scopes
        the read to that owner; ``field`` is accepted for the seam but v1 stores ONE
        credential per server (single-secret image servers, N4-D-2 static-token shape) — the
        credential maps to the server's sole declared secret env in :func:`build_spawn_env`.
        Transient in-memory decrypt; never persisted, logged, or placed in a return path
        other than this value.
        """
        del owner_id, field  # RLS scopes the owner; v1 = one credential per server
        return mcp_store.decrypted_credential_for_server(
            rls_engine=self._rls, config=self._config, server_name=server_name
        )


def build_spawn_env(
    resolver: FernetGatewaySecretResolver,
    *,
    owner_id: str,
    entry: MCPServerCatalogEntry,
) -> dict[str, str]:
    """Build the ``{env_var: secret}`` map to inject at Machine spawn (N6-D-2).

    Maps the tenant's stored credential onto the server's declared secret env var
    (``MCPSecretField.env``). A server with **no** declared secret (e.g. ``google-flights``)
    yields ``{}`` — nothing to inject. **v1 supports single-secret image servers**: the
    Spec-30 store holds one credential per server (N4-D-2), so a server declaring more than
    one secret gets only its first env populated (logged); multi-secret images are a
    documented v1 limitation (extending the store to a secret map is a follow-on).

    The returned map is handed to :meth:`PerTenantMCPRuntime.ensure`'s ``secret_env`` and
    lives only in memory + the Machine env — never a returned instance, log, or error body.
    """
    env: dict[str, str] = {}
    for field in entry.secrets[:1]:
        value = resolver.resolve(owner_id=owner_id, server_name=entry.name, field=field)
        if value is not None:
            env[field.env] = value
    if len(entry.secrets) > 1:
        _LOG.warning(
            "mcp image server declares multiple secrets; v1 injects only the first",
            server=entry.name,
            declared=len(entry.secrets),
        )
    return env
