"""Integration — the Fly per-tenant MCP runtime (Spec N6, T2b).

Real DB (the instance store + RLS) + an in-memory fake Fly substrate. The LOAD-BEARING
test is the crash-window adoption real-transition (N6-D-7a condition 1): a real fault
injected between the real ``create_machine`` and the real ``set_fly_machine_id`` leaves an
orphaned Machine; the next real ``ensure`` ADOPTS it by its derived name → zero
double-spawn. No simulated end-state — the recovery is driven through the real code path.

Also covers: happy spawn → running, idempotent re-ensure, spawn-env secret injection
(N6-D-2, the value reaches the Machine env, not the returned instance), stopped→respawn
(condition 2), and cross-tenant ``reap_idle`` (condition 3).

Needs Postgres + ``persona_app`` (``APP_DATABASE_URL``); ``integration``-marked. Run with
``PERSONA_TEST_DB=1``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from persona_api.mcp import runtime_store
from persona_api.mcp.fly import FlyMachine
from persona_api.mcp.fly_runtime import FlyPerTenantMCPRuntime, derive_machine_name
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


class _FakeFly:
    """In-memory Fly Machines substrate — records creates so double-spawn is observable."""

    def __init__(self) -> None:
        self._by_id: dict[str, dict[str, object]] = {}
        self._name_to_id: dict[str, str] = {}
        self.create_count = 0
        self.stopped_ids: list[str] = []

    async def get_machine_by_name(self, name: str) -> FlyMachine | None:
        mid = self._name_to_id.get(name)
        if mid is None:
            return None
        return FlyMachine(id=mid, name=name, state=str(self._by_id[mid]["state"]))

    async def create_machine(self, *, name: str, image: str, env: Mapping[str, str]) -> FlyMachine:
        self.create_count += 1
        mid = f"m-{self.create_count}"
        self._by_id[mid] = {"name": name, "state": "started", "env": dict(env), "image": image}
        self._name_to_id[name] = mid
        return FlyMachine(id=mid, name=name, state="started")

    async def start_machine(self, machine_id: str) -> None:
        self._by_id[machine_id]["state"] = "started"

    async def wait_started(self, machine_id: str) -> None:
        del machine_id  # fake: nothing to wait for

    async def stop_machine(self, machine_id: str) -> None:
        self.stopped_ids.append(machine_id)
        if machine_id in self._by_id:
            self._by_id[machine_id]["state"] = "stopped"

    async def destroy_machine(self, machine_id: str) -> None:
        self._by_id.pop(machine_id, None)
        self._name_to_id = {n: i for n, i in self._name_to_id.items() if i != machine_id}

    def env_of(self, name: str) -> dict[str, str]:
        return dict(self._by_id[self._name_to_id[name]]["env"])  # type: ignore[arg-type]


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


def _seed(superuser: Engine, *, owner: str, server_id: str) -> None:
    with superuser.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, :e) ON CONFLICT DO NOTHING"),
            {"o": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text(
                "INSERT INTO user_mcp_servers (id, owner_id, name, url, auth_method) "
                "VALUES (:s, :o, 'google-flights', 'https://x.test/mcp', 'none') "
                "ON CONFLICT DO NOTHING"
            ),
            {"s": server_id, "o": owner},
        )


def _runtime(app_engine: Engine, bypass: Engine, fly: _FakeFly) -> FlyPerTenantMCPRuntime:
    # These tests exercise the runtime mechanics, not vetting → a permissive gate.
    return FlyPerTenantMCPRuntime(
        rls_engine=app_engine,
        bypass_engine=bypass,
        fly=fly,
        app="op-mcp",
        port=8000,
        is_runnable_image=lambda _image: True,
    )


class TestEnsureSpawn:
    @pytest.mark.asyncio
    async def test_happy_spawn_reaches_running_and_creates_once(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        fly = _FakeFly()
        rt = _runtime(app_engine, migrated_engine, fly)
        with _acting_as("owner_a"):
            out = await rt.ensure(
                owner_id="owner_a", server_id="srv_a", image="mcp/google-flights", secret_env={}
            )
        assert out.state == "running"
        assert out.endpoint_url == f"http://{out.fly_machine_id}.vm.op-mcp.internal:8000/mcp"
        assert fly.create_count == 1

    @pytest.mark.asyncio
    async def test_reensure_on_running_does_not_respawn(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        fly = _FakeFly()
        rt = _runtime(app_engine, migrated_engine, fly)
        with _acting_as("owner_a"):
            await rt.ensure(owner_id="owner_a", server_id="srv_a", image="mcp/x", secret_env={})
            again = await rt.ensure(
                owner_id="owner_a", server_id="srv_a", image="mcp/x", secret_env={}
            )
        assert again.state == "running"
        assert fly.create_count == 1  # adopted its own live Machine, no second spawn

    @pytest.mark.asyncio
    async def test_secret_reaches_machine_env_not_the_returned_instance(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        fly = _FakeFly()
        rt = _runtime(app_engine, migrated_engine, fly)
        with _acting_as("owner_a"):
            out = await rt.ensure(
                owner_id="owner_a",
                server_id="srv_a",
                image="mcp/x",
                secret_env={"FLIGHTS_API_KEY": "s3cr3t"},
            )
        name = derive_machine_name("owner_a", "srv_a")
        # Injected at spawn (N6-D-2)...
        assert fly.env_of(name) == {"FLIGHTS_API_KEY": "s3cr3t"}
        # ...and NEVER in the returned instance.
        assert "s3cr3t" not in out.model_dump_json()


class TestCrashWindowAdoption:
    @pytest.mark.asyncio
    async def test_reensure_after_crash_between_create_and_persist_adopts(
        self, app_engine: Engine, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # THE load-bearing proof (N6-D-7a condition 1). A real fault between the real
        # create_machine and the real set_fly_machine_id → re-ensure ADOPTS by name.
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        fly = _FakeFly()
        rt = _runtime(app_engine, migrated_engine, fly)

        real_set = runtime_store.set_fly_machine_id
        calls = {"n": 0}

        def _crash_once(**kw: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated process death after Fly-create, before persist")
            real_set(**kw)  # type: ignore[arg-type]

        monkeypatch.setattr(runtime_store, "set_fly_machine_id", _crash_once)

        # First ensure dies in the crash window (the Machine got created, the row didn't).
        with _acting_as("owner_a"), pytest.raises(RuntimeError):
            await rt.ensure(owner_id="owner_a", server_id="srv_a", image="mcp/x", secret_env={})
        assert fly.create_count == 1
        with _acting_as("owner_a"):
            mid = runtime_store.get(rls_engine=app_engine, owner_id="owner_a", server_id="srv_a")
        assert mid is not None
        assert mid.state == "pending"  # never advanced — the id was never persisted
        assert mid.fly_machine_id is None

        # Process "restarts", the persona re-resolves → must ADOPT the orphan, not re-create.
        with _acting_as("owner_a"):
            out = await rt.ensure(
                owner_id="owner_a", server_id="srv_a", image="mcp/x", secret_env={}
            )
        assert out.state == "running"
        assert fly.create_count == 1  # ← ZERO double-spawn: adopted by derived name


class TestSpawnBoundaryVetting:
    @pytest.mark.asyncio
    async def test_devetted_image_fails_closed_at_spawn_no_create(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        # The TOCTOU guard (N6-D-4): the row exists (assigned), but the image was pulled from
        # the allow-list before spawn → ensure must fail CLOSED, never create/adopt a Machine.
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        fly = _FakeFly()
        rt = FlyPerTenantMCPRuntime(
            rls_engine=app_engine,
            bypass_engine=migrated_engine,
            fly=fly,
            app="op-mcp",
            port=8000,
            is_runnable_image=lambda _image: False,  # de-vetted since assign
        )
        with _acting_as("owner_a"):
            out = await rt.ensure(
                owner_id="owner_a", server_id="srv_a", image="mcp/x", secret_env={}
            )
        assert out.state == "failed"
        assert out.fly_machine_id is None
        assert fly.create_count == 0  # ← nothing spawned: fail-closed
        # The row records the unvetted reason for the not-connected signal.
        with migrated_engine.begin() as conn:
            reason = conn.execute(
                text("SELECT state_reason FROM mcp_runtime_instances WHERE owner_id='owner_a'")
            ).scalar_one()
        assert reason == "unvetted"


class TestStoppedRespawnAndReap:
    @pytest.mark.asyncio
    async def test_stop_then_reensure_respawns(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        _seed(migrated_engine, owner="owner_a", server_id="srv_a")
        fly = _FakeFly()
        rt = _runtime(app_engine, migrated_engine, fly)
        with _acting_as("owner_a"):
            await rt.ensure(owner_id="owner_a", server_id="srv_a", image="mcp/x", secret_env={})
            await rt.stop(owner_id="owner_a", server_id="srv_a")
            gone = runtime_store.get(rls_engine=app_engine, owner_id="owner_a", server_id="srv_a")
            assert gone is not None
            assert gone.state == "stopped"
            # Re-resolve → the dead row re-drives to pending then back up to running.
            back = await rt.ensure(
                owner_id="owner_a", server_id="srv_a", image="mcp/x", secret_env={}
            )
        assert back.state == "running"

    @pytest.mark.asyncio
    async def test_reap_idle_stops_all_tenants(
        self, app_engine: Engine, migrated_engine: Engine
    ) -> None:
        for owner, srv in (("owner_a", "srv_a"), ("owner_b", "srv_b")):
            _seed(migrated_engine, owner=owner, server_id=srv)
        fly = _FakeFly()
        rt = _runtime(app_engine, migrated_engine, fly)
        for owner, srv in (("owner_a", "srv_a"), ("owner_b", "srv_b")):
            with _acting_as(owner):
                await rt.ensure(owner_id=owner, server_id=srv, image="mcp/x", secret_env={})
        # Age both rows past the idle window.
        with migrated_engine.begin() as conn:
            conn.execute(
                text("UPDATE mcp_runtime_instances SET last_used_at = :old"),
                {"old": datetime(2020, 1, 1, tzinfo=UTC)},
            )
        reaped = await rt.reap_idle(now=datetime.now(UTC), idle_timeout_s=300.0)
        assert reaped == 2  # cross-tenant: one sweep reaped BOTH tenants
        assert len(fly.stopped_ids) == 2
        # Both rows are now stopped (restartable on the next resolve).
        with migrated_engine.begin() as conn:
            states = [
                r[0] for r in conn.execute(text("SELECT state FROM mcp_runtime_instances")).all()
            ]
        assert states == ["stopped", "stopped"]
