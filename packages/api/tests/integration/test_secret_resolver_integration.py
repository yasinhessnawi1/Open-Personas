"""Integration — per-user secret resolution + spawn injection, isolated (Spec N6, T3).

Real Spec-30 Fernet store + DB + a fake Fly substrate. Proves the load-bearing security
criteria:

- **#3 secret-never-in-context (structural):** the resolved credential appears in the
  Machine's spawn env and NOWHERE in the returned instance;
- **#2 isolation non-vacuous:** two tenants, two DIFFERENT credentials, ONE server name →
  two Machines, each carrying only its own secret; tenant A's Machine env never holds B's;
- **RLS:** ``resolve`` under tenant A only ever reads A's credential.

Reuses N4's store + its single ``MCP_CREDENTIAL_KEY`` (no second key, D-N1-5 / N4-D-1).
Needs Postgres + ``persona_app`` (``APP_DATABASE_URL``); ``integration``-marked.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet
from persona.tools.mcp.catalog import MCPSecretField, MCPServerCatalogEntry
from persona_api.config import APIConfig
from persona_api.mcp.crypto import cipher_from_config
from persona_api.mcp.fly import FlyMachine
from persona_api.mcp.fly_runtime import FlyPerTenantMCPRuntime, derive_machine_name
from persona_api.mcp.secret_resolver import FernetGatewaySecretResolver, build_spawn_env
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration

_KEY = Fernet.generate_key().decode()
_CONFIG = APIConfig(mcp_credential_key=_KEY)
_ENV_VAR = "FLIGHTS_API_KEY"


def _entry() -> MCPServerCatalogEntry:
    return MCPServerCatalogEntry(
        name="google-flights",
        description="flights",
        kind="external",
        risk="medium",
        server_type="server",
        image="mcp/google-flights",
        secrets=(MCPSecretField(name="flights.api_key", env=_ENV_VAR),),
    )


class _FakeFly:
    def __init__(self) -> None:
        self.env_by_name: dict[str, dict[str, str]] = {}
        self._by_id: dict[str, str] = {}

    async def get_machine_by_name(self, name: str) -> FlyMachine | None:
        for mid, nm in self._by_id.items():
            if nm == name:
                return FlyMachine(id=mid, name=name, state="started")
        return None

    async def create_machine(self, *, name: str, image: str, env: Mapping[str, str]) -> FlyMachine:
        del image
        mid = f"m-{len(self._by_id) + 1}"
        self._by_id[mid] = name
        self.env_by_name[name] = dict(env)
        return FlyMachine(id=mid, name=name, state="started")

    async def start_machine(self, machine_id: str) -> None:
        del machine_id

    async def wait_started(self, machine_id: str) -> None:
        del machine_id

    async def stop_machine(self, machine_id: str) -> None:
        del machine_id

    async def destroy_machine(self, machine_id: str) -> None:
        del machine_id


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


def _seed_user(superuser: Engine, owner: str) -> None:
    with superuser.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, :e) ON CONFLICT DO NOTHING"),
            {"o": owner, "e": f"{owner}@x"},
        )


def _create_credentialed_server(superuser: Engine, owner: str, credential: str) -> str:
    """Seed a bearer-credentialed BYO server named 'google-flights'; return its id.

    Inserts via the superuser engine with the credential pre-encrypted under the SAME
    Spec-30 Fernet cipher the resolver reads with — so the test exercises the resolver's
    read+decrypt path directly, offline (no SSRF/network dependency on ``create_server``).
    """
    cipher = cipher_from_config(_CONFIG)
    assert cipher is not None
    with superuser.begin() as conn:
        return str(
            conn.execute(
                text(
                    "INSERT INTO user_mcp_servers "
                    "(owner_id, name, url, auth_method, credentials_encrypted) "
                    "VALUES (:o, 'google-flights', 'https://example.com/mcp', 'bearer', :c) "
                    "RETURNING id"
                ),
                {"o": owner, "c": cipher.encrypt(credential)},
            ).scalar_one()
        )


class TestResolve:
    def test_resolve_returns_the_decrypted_credential(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed_user(migrated_engine, "owner_a")
        _create_credentialed_server(migrated_engine, "owner_a", "s3cr3t")
        resolver = FernetGatewaySecretResolver(rls_engine=app_engine, config=_CONFIG)
        with _acting_as("owner_a"):
            got = resolver.resolve(
                owner_id="owner_a",
                server_name="google-flights",
                field=MCPSecretField(name="flights.api_key", env=_ENV_VAR),
            )
        assert got == "s3cr3t"

    def test_resolve_is_rls_scoped_to_the_caller(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        # Same server NAME, two tenants, two different secrets.
        for owner, cred in (("owner_a", "aaa"), ("owner_b", "bbb")):
            _seed_user(migrated_engine, owner)
            _create_credentialed_server(migrated_engine, owner, cred)
        resolver = FernetGatewaySecretResolver(rls_engine=app_engine, config=_CONFIG)
        field = MCPSecretField(name="flights.api_key", env=_ENV_VAR)
        with _acting_as("owner_a"):
            a = resolver.resolve(owner_id="owner_a", server_name="google-flights", field=field)
        with _acting_as("owner_b"):
            b = resolver.resolve(owner_id="owner_b", server_name="google-flights", field=field)
        assert a == "aaa"
        assert b == "bbb"  # each tenant sees only its own credential (RLS)


class TestSecretInjectionIsolation:
    @pytest.mark.asyncio
    async def test_secret_reaches_machine_env_not_the_returned_instance(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed_user(migrated_engine, "owner_a")
        _create_credentialed_server(migrated_engine, "owner_a", "s3cr3t")
        resolver = FernetGatewaySecretResolver(rls_engine=app_engine, config=_CONFIG)
        fly = _FakeFly()
        rt = FlyPerTenantMCPRuntime(
            rls_engine=app_engine,
            bypass_engine=migrated_engine,
            fly=fly,
            app="op",
            port=8000,
            is_runnable_image=lambda _image: True,
        )
        with _acting_as("owner_a"):
            env = build_spawn_env(resolver, owner_id="owner_a", entry=_entry())
            out = await rt.ensure(
                owner_id="owner_a",
                server_id=_server_id(migrated_engine, "owner_a"),
                image="mcp/google-flights",
                secret_env=env,
            )
        name = derive_machine_name("owner_a", _server_id(migrated_engine, "owner_a"))
        # Injected at spawn...
        assert fly.env_by_name[name] == {_ENV_VAR: "s3cr3t"}
        # ...and structurally absent from the returned instance (acceptance #3).
        assert "s3cr3t" not in out.model_dump_json()

    @pytest.mark.asyncio
    async def test_two_tenants_two_secrets_two_isolated_machines(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        # Acceptance #2, non-vacuous: two tenants, two credentials, ONE server name.
        for owner, cred in (("owner_a", "aaa-secret"), ("owner_b", "bbb-secret")):
            _seed_user(migrated_engine, owner)
            _create_credentialed_server(migrated_engine, owner, cred)
        resolver = FernetGatewaySecretResolver(rls_engine=app_engine, config=_CONFIG)
        fly = _FakeFly()
        rt = FlyPerTenantMCPRuntime(
            rls_engine=app_engine,
            bypass_engine=migrated_engine,
            fly=fly,
            app="op",
            port=8000,
            is_runnable_image=lambda _image: True,
        )
        names: dict[str, str] = {}
        for owner in ("owner_a", "owner_b"):
            sid = _server_id(migrated_engine, owner)
            with _acting_as(owner):
                env = build_spawn_env(resolver, owner_id=owner, entry=_entry())
                await rt.ensure(
                    owner_id=owner, server_id=sid, image="mcp/google-flights", secret_env=env
                )
            names[owner] = derive_machine_name(owner, sid)
        env_a = fly.env_by_name[names["owner_a"]]
        env_b = fly.env_by_name[names["owner_b"]]
        # Each Machine carries ONLY its own tenant's secret — the boundary is real.
        assert env_a == {_ENV_VAR: "aaa-secret"}
        assert env_b == {_ENV_VAR: "bbb-secret"}
        assert "bbb-secret" not in str(env_a)
        assert "aaa-secret" not in str(env_b)
        assert names["owner_a"] != names["owner_b"]  # distinct Machines


# --- helpers that reconcile the BYO server id into the runtime's server_id -------------


def _server_id(superuser: Engine, owner: str) -> str:
    with superuser.begin() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT id FROM user_mcp_servers "
                    "WHERE owner_id = :o AND name = 'google-flights'"
                ),
                {"o": owner},
            ).scalar_one()
        )
