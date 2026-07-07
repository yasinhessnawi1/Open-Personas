"""Integration — per-tenant MCP runtime-instance persistence (Spec N6, T2a).

Proves the durable layer the Fly runtime (T2b) reconciles against:

- the ``UNIQUE(owner_id, server_id)`` idempotency key (N6-D-7a condition 1) —
  ``ensure`` never makes a second row for the same (tenant, server);
- the ``stopped``/``failed`` → ``pending`` re-drive on re-``ensure`` (condition 2);
- RLS non-vacuous — tenant A's instance row is invisible to tenant B under
  ``persona_app`` (the per-tenant engine contract);
- the reaper read is CROSS-TENANT under the bypass engine (condition 3) — one sweep
  sees every tenant's idle instances, which a per-tenant engine cannot.

Needs a real Postgres + the non-superuser ``persona_app`` role (``APP_DATABASE_URL``);
marked ``integration`` (skipped in the unit suite). Run with ``PERSONA_TEST_DB=1``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from persona_api.mcp import runtime_store
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    # ``migrated_engine`` (superuser) builds schema + RLS + truncates; depend on it so
    # the schema exists before the persona_app engine connects.
    _ = migrated_engine
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url)
    yield engine
    engine.dispose()


@contextmanager
def _acting_as(owner: str) -> Iterator[None]:
    """Bind the RLS ``current_user_id`` ContextVar so ``persona_app`` scopes to ``owner``."""
    token = current_user_id.set(owner)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _seed(superuser: Engine, *, owner: str, server_id: str) -> None:
    """Seed the FK parents (a user + one of its BYO servers) via the superuser engine."""
    with superuser.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, :e) ON CONFLICT DO NOTHING"),
            {"o": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text(
                "INSERT INTO user_mcp_servers (id, owner_id, name, url, auth_method) "
                "VALUES (:s, :o, :n, :u, 'none') ON CONFLICT DO NOTHING"
            ),
            {"s": server_id, "o": owner, "n": "google-flights", "u": "https://example.test/mcp"},
        )


class TestEnsureIdempotency:
    def test_ensure_is_idempotent_on_owner_server(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        with _acting_as("owner_a"):
            first = runtime_store.ensure(
                rls_engine=app_engine, owner_id="owner_a", server_id="srv_a", image="mcp/x"
            )
            second = runtime_store.ensure(
                rls_engine=app_engine, owner_id="owner_a", server_id="srv_a", image="mcp/x"
            )
        assert first.state == "pending"
        assert second.state == "pending"
        # Exactly ONE row despite two ensures (the UNIQUE key held).
        with migrated_engine.begin() as conn:
            n = conn.execute(
                text("SELECT count(*) FROM mcp_runtime_instances WHERE owner_id = 'owner_a'")
            ).scalar_one()
        assert n == 1

    def test_ensure_redrives_stopped_row_to_pending(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        with _acting_as("owner_a"):
            runtime_store.ensure(
                rls_engine=app_engine, owner_id="owner_a", server_id="srv_a", image="mcp/x"
            )
            runtime_store.mark_running(
                rls_engine=app_engine,
                owner_id="owner_a",
                server_id="srv_a",
                endpoint_url="http://h/mcp",
            )
        # Reap it (cross-tenant path).
        inst_id = _one_id(migrated_engine, "owner_a")
        runtime_store.mark_stopped(bypass_engine=migrated_engine, instance_id=inst_id)
        # Re-resolve → the dead row must transition BACK to pending (condition 2),
        # not merely bump last_used_at.
        with _acting_as("owner_a"):
            redriven = runtime_store.ensure(
                rls_engine=app_engine, owner_id="owner_a", server_id="srv_a", image="mcp/x"
            )
        assert redriven.state == "pending"
        assert redriven.endpoint_url is None
        with migrated_engine.begin() as conn:
            reason = conn.execute(
                text("SELECT state_reason FROM mcp_runtime_instances WHERE owner_id='owner_a'")
            ).scalar_one()
        assert reason is None  # the stopped reason was cleared on re-drive

    def test_ensure_redrives_failed_row_to_pending(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        with _acting_as("owner_a"):
            runtime_store.ensure(
                rls_engine=app_engine, owner_id="owner_a", server_id="srv_a", image="mcp/x"
            )
            runtime_store.mark_state(
                rls_engine=app_engine,
                owner_id="owner_a",
                server_id="srv_a",
                state="failed",
                reason="spawn_failed",
            )
            redriven = runtime_store.ensure(
                rls_engine=app_engine, owner_id="owner_a", server_id="srv_a", image="mcp/x"
            )
        assert redriven.state == "pending"


class TestRlsIsolation:
    def test_owner_b_cannot_see_owner_a_instance(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        with _acting_as("owner_a"):
            runtime_store.ensure(
                rls_engine=app_engine, owner_id="owner_a", server_id="srv_a", image="mcp/x"
            )
        # Owner B, under persona_app, must not see A's row (RLS non-vacuous).
        with _acting_as("owner_b"):
            got = runtime_store.get(rls_engine=app_engine, owner_id="owner_a", server_id="srv_a")
        assert got is None


class TestCrossTenantReaper:
    def test_list_idle_running_sees_all_tenants(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        # Two tenants, each a running instance, both idle past the deadline.
        for owner, srv in (("owner_a", "srv_a"), ("owner_b", "srv_b")):
            _seed(migrated_engine, owner=owner, server_id=srv)
            with _acting_as(owner):
                runtime_store.ensure(
                    rls_engine=app_engine, owner_id=owner, server_id=srv, image="mcp/x"
                )
                runtime_store.mark_running(
                    rls_engine=app_engine,
                    owner_id=owner,
                    server_id=srv,
                    endpoint_url=f"http://{owner}/mcp",
                )
        # Age both rows well past any deadline (superuser update, no RLS).
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE mcp_runtime_instances SET last_used_at = :old"),
                {"old": datetime(2020, 1, 1, tzinfo=UTC)},
            )
        deadline = datetime.now(UTC) - timedelta(seconds=300)
        idle = runtime_store.list_idle_running(bypass_engine=migrated_engine, deadline=deadline)
        owners = {r["owner_id"] for r in idle}
        # The CROSS-TENANT guarantee (condition 3): one sweep sees BOTH tenants.
        assert owners == {"owner_a", "owner_b"}

    def test_touch_keeps_a_fresh_instance_out_of_the_reap(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        with _acting_as("owner_a"):
            runtime_store.ensure(
                rls_engine=app_engine, owner_id="owner_a", server_id="srv_a", image="mcp/x"
            )
            runtime_store.mark_running(
                rls_engine=app_engine,
                owner_id="owner_a",
                server_id="srv_a",
                endpoint_url="http://h/mcp",
            )
            runtime_store.touch(rls_engine=app_engine, owner_id="owner_a", server_id="srv_a")
        # Deadline in the past → a just-touched instance is NOT idle.
        deadline = datetime.now(UTC) - timedelta(seconds=300)
        idle = runtime_store.list_idle_running(bypass_engine=migrated_engine, deadline=deadline)
        assert idle == []


def _one_id(superuser: Engine, owner: str) -> str:
    with superuser.begin() as conn:
        return str(
            conn.execute(
                text("SELECT id FROM mcp_runtime_instances WHERE owner_id = :o"), {"o": owner}
            ).scalar_one()
        )
