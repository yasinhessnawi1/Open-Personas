"""The Fly Machines REST seam for the per-tenant MCP runtime (Spec N6, T2b, N6-D-1).

A thin adapter over the Fly Machines API (``https://api.machines.dev``) — the substrate
that boots one image-MCP server per (tenant, server) as a Firecracker microVM (N6-D-1).
It is deliberately a **Protocol + one httpx implementation** so the runtime
(:mod:`persona_api.mcp.fly_runtime`) composes the interface and tests inject an in-memory
fake — no live Fly in the unit/integration suite (the real round-trip is the T8 operator
leg). ``httpx`` only, no Fly SDK (N6-D-9).

Only the lifecycle the runtime needs: reconcile-by-name (the crash-window adoption anchor,
N6-D-7a condition 1), create-with-env (the secret injection point, N6-D-2), start/stop/
destroy, and wait-started. Secrets ride ``create``'s ``env`` map (injected at spawn) and
never appear in this module's return values or logs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict

from persona_api.errors import MCPRuntimeSubstrateError

if TYPE_CHECKING:
    from collections.abc import Mapping

    import httpx

__all__ = [
    "FlyMachine",
    "FlyMachinesClient",
    "HttpxFlyMachinesClient",
]

_LOG = get_logger("api.mcp.fly")

#: Guest sizing for an image-MCP Machine (N6-R-1: 256 MB is the per-tenant cost floor).
_GUEST = {"cpu_kind": "shared", "cpus": 1, "memory_mb": 256}


class FlyMachine(BaseModel):
    """The subset of a Fly Machine the runtime reasons about (frozen; never a secret)."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    name: str
    state: str


@runtime_checkable
class FlyMachinesClient(Protocol):
    """The Fly Machines lifecycle the runtime drives (substrate seam).

    Every method raises :class:`MCPRuntimeSubstrateError` on a transport/API failure so
    the runtime can fail-soft to ``not_connected(reason="fly_outage")`` (N6-D-6) rather
    than propagate an httpx error into the persona load.
    """

    async def get_machine_by_name(self, name: str) -> FlyMachine | None:
        """Return the Machine named ``name`` (the derived (owner, server) name), or ``None``.

        The reconcile/adopt primitive (N6-D-7a condition 1): a crashed spawn that created
        a Machine but never persisted its id is recovered here — the next ``ensure``
        recomputes the deterministic name and adopts the orphan instead of re-creating.
        """
        ...

    async def create_machine(self, *, name: str, image: str, env: Mapping[str, str]) -> FlyMachine:
        """Create + auto-start a Machine running ``image`` with ``env`` injected at spawn.

        ``env`` carries the per-user secret resolved server-side (N6-D-2); it reaches the
        MCP process's environment and NEVER this method's return value or any log line.
        """
        ...

    async def start_machine(self, machine_id: str) -> None:
        """Start a stopped Machine (idempotent on an already-started Machine)."""
        ...

    async def wait_started(self, machine_id: str) -> None:
        """Block until the Machine reaches ``started`` (bounded), or raise on timeout."""
        ...

    async def stop_machine(self, machine_id: str) -> None:
        """Stop a running Machine (idle-reap / unassign). Idempotent."""
        ...

    async def destroy_machine(self, machine_id: str) -> None:
        """Destroy a Machine (hard teardown). Idempotent."""
        ...


class HttpxFlyMachinesClient:
    """The live Fly Machines client over ``httpx`` (N6-D-9: no Fly SDK).

    Args:
        app: The Fly app the per-tenant Machines live in (operator config).
        token: The Fly API token (``FLY_API_TOKEN``); ``repr``-hidden by never being
            stored anywhere it is logged. Sent as ``Authorization: Bearer``.
        client: An injected :class:`httpx.AsyncClient` (DI — the composition root owns
            its lifecycle; tests pass a transport-mocked client).
        base_url: The Machines API root (default the public endpoint).
        wait_timeout_s: Bound for :meth:`wait_started`.
    """

    def __init__(
        self,
        *,
        app: str,
        token: str,
        client: httpx.AsyncClient,
        base_url: str = "https://api.machines.dev",
        wait_timeout_s: float = 30.0,
    ) -> None:
        self._app = app
        self._token = token
        self._client = client
        self._base = base_url.rstrip("/")
        self._wait_timeout_s = wait_timeout_s

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _machines_url(self, *parts: str) -> str:
        tail = "/".join(("v1", "apps", self._app, "machines", *parts))
        return f"{self._base}/{tail}"

    async def get_machine_by_name(self, name: str) -> FlyMachine | None:
        resp = await self._request("GET", self._machines_url())
        for m in resp.json():
            if m.get("name") == name:
                return FlyMachine.model_validate(m)
        return None

    async def create_machine(self, *, name: str, image: str, env: Mapping[str, str]) -> FlyMachine:
        body = {
            "name": name,
            "config": {
                "image": image,
                "env": dict(env),  # spawn-time secret injection (N6-D-2)
                "guest": _GUEST,
                "auto_destroy": False,
            },
        }
        resp = await self._request("POST", self._machines_url(), json=body)
        # Log the Machine identity ONLY — never ``env`` (it holds the secret).
        machine = FlyMachine.model_validate(resp.json())
        _LOG.info("fly machine created", name=name, machine_id=machine.id, image=image)
        return machine

    async def start_machine(self, machine_id: str) -> None:
        await self._request("POST", self._machines_url(machine_id, "start"))

    async def wait_started(self, machine_id: str) -> None:
        url = self._machines_url(machine_id, "wait")
        await self._request(
            "GET", url, params={"state": "started", "timeout": int(self._wait_timeout_s)}
        )

    async def stop_machine(self, machine_id: str) -> None:
        await self._request("POST", self._machines_url(machine_id, "stop"))

    async def destroy_machine(self, machine_id: str) -> None:
        await self._request("DELETE", self._machines_url(machine_id), params={"force": "true"})

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: object | None = None,
        params: Mapping[str, str | int] | None = None,
    ) -> httpx.Response:
        """One authenticated call; map any transport/HTTP error to a domain error."""
        import httpx  # lazy — keep the api import graph light

        try:
            resp = await self._client.request(
                method, url, headers=self._headers, json=json, params=params
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # Never include the response body (a create echoes ``env`` → the secret).
            raise MCPRuntimeSubstrateError(
                "fly machines API call failed",
                context={"method": method, "status": _status_of(exc)},
            ) from exc
        return resp


def _status_of(exc: Exception) -> str:
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        return str(exc.response.status_code)
    return "transport"
