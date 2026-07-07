"""Unit tests — the per-tenant MCP runtime vocabulary + Protocol (Spec N6, T1).

Runtime-agnostic types only (no Fly, no DB): the not-connected signal value
(:class:`MCPServerConnection`), the instance value (:class:`MCPRuntimeInstance`) and
its state→signal mapping, and Protocol conformance for a fake substrate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona_api.mcp.runtime import (
    MCPRuntimeInstance,
    MCPServerConnection,
    PerTenantMCPRuntime,
)
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping


class TestMCPServerConnection:
    def test_connected_carries_no_reason(self) -> None:
        c = MCPServerConnection.make_connected("google-flights")
        assert c.connected is True
        assert c.reason is None
        assert c.server_name == "google-flights"

    def test_not_connected_carries_its_reason(self) -> None:
        c = MCPServerConnection.make_not_connected("google-flights", "not_enabled")
        assert c.connected is False
        assert c.reason == "not_enabled"

    def test_connected_with_a_reason_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MCPServerConnection(server_name="x", connected=True, reason="stopped")

    def test_not_connected_without_a_reason_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MCPServerConnection(server_name="x", connected=False, reason=None)

    def test_is_frozen(self) -> None:
        c = MCPServerConnection.make_connected("x")
        with pytest.raises(ValidationError):
            c.connected = False  # type: ignore[misc]

    def test_unknown_reason_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MCPServerConnection(
                server_name="x",
                connected=False,
                reason="totally_made_up",  # type: ignore[arg-type]
            )


class TestMCPRuntimeInstanceToConnection:
    def _instance(self, **kw: object) -> MCPRuntimeInstance:
        base: dict[str, object] = {
            "owner_id": "u1",
            "server_id": "s1",
            "fly_machine_name": "op-mcp-u1-s1",
        }
        base.update(kw)
        return MCPRuntimeInstance(**base)  # type: ignore[arg-type]

    def test_running_with_endpoint_maps_to_connected(self) -> None:
        inst = self._instance(state="running", endpoint_url="http://x/mcp")
        assert inst.to_connection("google-flights").connected is True

    def test_running_without_endpoint_maps_to_spawn_failed(self) -> None:
        # Defensive: a "running" row with no endpoint is not truly serving.
        inst = self._instance(state="running", endpoint_url=None)
        conn = inst.to_connection("google-flights")
        assert conn.connected is False
        assert conn.reason == "spawn_failed"

    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            ("pending", "starting"),
            ("starting", "starting"),
            ("stopped", "stopped"),
            ("failed", "spawn_failed"),
        ],
    )
    def test_non_running_states_map_to_their_reason(self, state: str, reason: str) -> None:
        inst = self._instance(state=state)  # type: ignore[arg-type]
        conn = inst.to_connection("google-flights")
        assert conn.connected is False
        assert conn.reason == reason

    def test_defaults_are_pending_and_secretless(self) -> None:
        inst = self._instance()
        assert inst.state == "pending"
        assert inst.fly_machine_id is None
        assert inst.endpoint_url is None
        # No secret field exists on the value at all (structural, N6-D-2).
        assert "secret" not in inst.model_dump()

    def test_is_frozen(self) -> None:
        inst = self._instance()
        with pytest.raises(ValidationError):
            inst.state = "running"  # type: ignore[misc]


class _FakeRuntime:
    """A minimal substrate that satisfies :class:`PerTenantMCPRuntime` structurally.

    Records that ``ensure`` never receives a secret it then leaks into the returned
    value (the injection-boundary contract, N6-D-2).
    """

    def __init__(self) -> None:
        self.reaped = 0

    async def ensure(
        self,
        *,
        owner_id: str,
        server_id: str,
        image: str,
        secret_env: Mapping[str, str],  # noqa: ARG002 — Protocol arg; the fake proves it is not leaked
    ) -> MCPRuntimeInstance:
        assert image  # the vetted image is required
        return MCPRuntimeInstance(
            owner_id=owner_id,
            server_id=server_id,
            fly_machine_name=f"op-mcp-{owner_id}-{server_id}",
            fly_machine_id="fly-123",
            endpoint_url="http://127.0.0.1:9/mcp",
            state="running",
        )

    async def stop(self, *, owner_id: str, server_id: str) -> None:
        del owner_id, server_id  # Protocol signature; the fake is a no-op
        return

    async def reap_idle(self, *, now: datetime, idle_timeout_s: float) -> int:
        del now, idle_timeout_s  # Protocol signature; the fake just counts calls
        self.reaped += 1
        return 0


class TestPerTenantMCPRuntimeProtocol:
    def test_fake_satisfies_the_protocol(self) -> None:
        assert isinstance(_FakeRuntime(), PerTenantMCPRuntime)

    @pytest.mark.asyncio
    async def test_ensure_returns_a_secretless_instance(self) -> None:
        rt = _FakeRuntime()
        inst = await rt.ensure(
            owner_id="u1",
            server_id="s1",
            image="mcp/google-flights",
            secret_env={"FLIGHTS_API_KEY": "super-secret-value"},
        )
        # The secret we passed in never appears in the returned value.
        assert "super-secret-value" not in inst.model_dump_json()
        assert inst.state == "running"

    @pytest.mark.asyncio
    async def test_reap_is_callable_with_pure_now(self) -> None:
        rt = _FakeRuntime()
        n = await rt.reap_idle(now=datetime(2026, 7, 5, tzinfo=UTC), idle_timeout_s=300.0)
        assert n == 0
        assert rt.reaped == 1
