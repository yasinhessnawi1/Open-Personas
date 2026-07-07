"""Unit — the per-tenant secret resolver seam + spawn-env builder (Spec N6, T3, N6-D-2).

No DB: proves the api-layer resolver structurally satisfies the N1 Protocol, that
``build_spawn_env`` maps the credential onto the declared secret env (and handles zero /
multi secret), and — adversarially — that a Fly error body echoing a secret never leaks it
into the raised :class:`MCPRuntimeSubstrateError` (acceptance #3, "not in any error body").
"""

from __future__ import annotations

import httpx
import pytest
from persona.tools.mcp.catalog import MCPSecretField, MCPServerCatalogEntry
from persona.tools.mcp.gateway_secrets import GatewaySecretResolver
from persona_api.errors import MCPRuntimeSubstrateError
from persona_api.mcp.fly import HttpxFlyMachinesClient
from persona_api.mcp.secret_resolver import FernetGatewaySecretResolver, build_spawn_env


def _entry(*secrets: MCPSecretField) -> MCPServerCatalogEntry:
    return MCPServerCatalogEntry(
        name="google-flights",
        description="flights",
        kind="external",
        risk="medium",
        server_type="server",
        image="mcp/google-flights",
        secrets=tuple(secrets),
    )


class _FakeResolver:
    """Returns a fixed value for any resolve (stands in for the Fernet-backed resolver)."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def resolve(self, *, owner_id: str, server_name: str, field: MCPSecretField) -> str | None:
        del owner_id, server_name, field
        return self._value


class TestResolverImplementsTheSeam:
    def test_fernet_resolver_satisfies_the_n1_protocol(self) -> None:
        # The api-layer resolver IS a GatewaySecretResolver (the N1 seam now implemented).
        resolver = FernetGatewaySecretResolver(rls_engine=None, config=None)  # type: ignore[arg-type]
        assert isinstance(resolver, GatewaySecretResolver)


class TestBuildSpawnEnv:
    def test_zero_secret_server_yields_empty_env(self) -> None:
        # google-flights declares no secret → nothing to inject.
        env = build_spawn_env(_FakeResolver("x"), owner_id="u", entry=_entry())  # type: ignore[arg-type]
        assert env == {}

    def test_single_secret_maps_credential_to_declared_env(self) -> None:
        field = MCPSecretField(name="flights.api_key", env="FLIGHTS_API_KEY")
        env = build_spawn_env(
            _FakeResolver("s3cr3t"),  # type: ignore[arg-type]
            owner_id="u",
            entry=_entry(field),
        )
        assert env == {"FLIGHTS_API_KEY": "s3cr3t"}

    def test_missing_credential_yields_empty_env(self) -> None:
        field = MCPSecretField(name="flights.api_key", env="FLIGHTS_API_KEY")
        env = build_spawn_env(_FakeResolver(None), owner_id="u", entry=_entry(field))  # type: ignore[arg-type]
        assert env == {}

    def test_multi_secret_injects_only_the_first_v1(self) -> None:
        f1 = MCPSecretField(name="a", env="A_ENV")
        f2 = MCPSecretField(name="b", env="B_ENV")
        env = build_spawn_env(
            _FakeResolver("v"),  # type: ignore[arg-type]
            owner_id="u",
            entry=_entry(f1, f2),
        )
        assert env == {"A_ENV": "v"}  # v1 single-secret bound; B_ENV not populated


class TestFlyErrorBodyNeverLeaksSecret:
    @pytest.mark.asyncio
    async def test_create_500_echoing_secret_does_not_leak_into_error(self) -> None:
        secret = "super-secret-token"

        def _handler(request: httpx.Request) -> httpx.Response:
            del request  # the handler ignores the request; it always 500s
            # A hostile / verbose Fly response that echoes the injected env back.
            return httpx.Response(500, json={"error": "boom", "config": {"env": {"K": secret}}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
        fly = HttpxFlyMachinesClient(app="op", token="t", client=client)
        with pytest.raises(MCPRuntimeSubstrateError) as ei:
            await fly.create_machine(name="opmcp-x", image="mcp/x", env={"K": secret})
        await client.aclose()
        # The secret rode in on the request env + came back in the response body, yet the
        # domain error carries only method + status — never the body (acceptance #3).
        assert secret not in str(ei.value)
        assert secret not in str(ei.value.context)
        assert ei.value.context.get("status") == "500"
