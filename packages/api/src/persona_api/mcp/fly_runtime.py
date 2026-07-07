"""The Fly-Machine per-tenant MCP runtime (Spec N6, T2b, N6-D-1 ratified substrate).

:class:`FlyPerTenantMCPRuntime` implements :class:`~persona_api.mcp.runtime.PerTenantMCPRuntime`
over the Fly Machines seam (:mod:`persona_api.mcp.fly`) + the instance store
(:mod:`persona_api.mcp.runtime_store`). One Fly Machine per (tenant, server) (N6-D-1a):
the Firecracker microVM boundary IS the tenant trust boundary.

The three binding conditions (N6-D-7a) live here:

1. **Crash-window idempotency.** The Machine name is a DETERMINISTIC function of
   ``(owner_id, server_id)`` (:func:`derive_machine_name`), so ``ensure`` reconciles by
   name and **adopts** an orphaned Machine (created but never persisted, because the
   process died in the window between ``create_machine`` and ``set_fly_machine_id``)
   rather than re-creating it → zero double-spawn.
2. **stopped/failed re-drive.** ``runtime_store.ensure`` re-drives a dead row to
   ``pending`` (SQL-level) so a reaped/failed instance re-spawns on the next resolve.
3. **Engine split.** ``ensure`` / ``stop`` run under the per-tenant ``rls_engine``
   (``persona_app``, RLS-scoped to the request's owner); ``reap_idle`` runs under the
   RLS-bypassing ``bypass_engine`` so ONE sweep reaps every tenant's idle Machines.

Fail-soft: any substrate error (:class:`MCPRuntimeSubstrateError`) marks the instance
``failed`` / ``fly_outage`` and returns it (the not-connected signal, N6-D-6) — a Fly
outage never propagates into the persona load.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from persona.logging import get_logger

from persona_api.errors import MCPRuntimeSubstrateError
from persona_api.mcp import runtime_store
from persona_api.mcp.runtime import MCPRuntimeInstance

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sqlalchemy import Engine

    from persona_api.mcp.fly import FlyMachinesClient

__all__ = ["FlyPerTenantMCPRuntime", "derive_machine_name"]

_LOG = get_logger("api.mcp.fly_runtime")

#: Fly Machine names must be short, lowercase, and start with a letter. A raw
#: ``{owner}-{server}`` (two 36-char UUIDs) overruns the limit, so derive a bounded,
#: charset-safe, COLLISION-RESISTANT name from a hash of the pair — a pure function, so
#: reconciliation recomputes the same name (N6-D-7a condition 1) with nothing stored.
_NAME_PREFIX = "opmcp"
_NAME_HASH_LEN = 20


def derive_machine_name(owner_id: str, server_id: str) -> str:
    """The deterministic Fly Machine name for one (tenant, server) instance.

    Pure: identical ``(owner_id, server_id)`` → identical name, on every process and
    every restart — the crash-window adoption anchor (N6-D-7a condition 1). Bounded
    length + ``[a-z0-9-]`` starting with a letter (Fly's name rules).
    """
    digest = hashlib.sha256(f"{owner_id}:{server_id}".encode()).hexdigest()
    return f"{_NAME_PREFIX}-{digest[:_NAME_HASH_LEN]}"


class FlyPerTenantMCPRuntime:
    """Runs one image-MCP server per (tenant, server) as a Fly Machine (N6-D-1).

    Args:
        rls_engine: The per-tenant (``persona_app``, RLS) engine for ``ensure``/``stop``
            — scoped to the request's owner via the ``current_user_id`` ContextVar.
        bypass_engine: The RLS-bypassing (superuser/voice) engine for ``reap_idle`` — it
            MUST scan cross-tenant (N6-D-7a condition 3).
        fly: The Fly Machines seam (live :class:`HttpxFlyMachinesClient`; tests inject a
            fake).
        app: The Fly app the per-tenant Machines live in (endpoint DNS + reap targeting).
        port: The port the in-Machine stdio→HTTP bridge serves ``/mcp`` on.
        is_runnable_image: The spawn-boundary vetting gate (N6-D-4) — given an image ref,
            returns whether it may run per-tenant under the CURRENT allow-list. Evaluated at
            every ``ensure`` so a de-vetted image fails closed at spawn (the TOCTOU guard),
            even for a server that passed the assign-boundary check earlier. The composition
            root (T5) builds it from :func:`persona_api.mcp.run_policy.runnable_images`.
    """

    def __init__(
        self,
        *,
        rls_engine: Engine,
        bypass_engine: Engine,
        fly: FlyMachinesClient,
        app: str,
        port: int,
        is_runnable_image: Callable[[str], bool],
        idle_timeout_s: float = 300.0,
        reap_interval_s: float = 60.0,
    ) -> None:
        self._rls = rls_engine
        self._bypass = bypass_engine
        self._fly = fly
        self._app = app
        self._port = port
        self._is_runnable_image = is_runnable_image
        self._idle_timeout_s = idle_timeout_s
        self._reap_interval_s = reap_interval_s
        # The pool-owned background idle-reaper (N6-D-3), spawned by :meth:`start`, cancelled
        # by :meth:`aclose` — mirroring SandboxPool's single-ownership reaper lifecycle.
        self._reaper_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        """Spawn the background idle-reaper (idempotent). Call from the FastAPI lifespan.

        The reaper sweeps CROSS-TENANT under ``bypass_engine`` (N6-D-7a condition 3) on the
        ``reap_interval_s`` cadence; it survives individual sweep errors.
        """
        if self._closed:
            msg = "runtime is closed; cannot start reaper"
            raise MCPRuntimeSubstrateError(msg, context={"reason": "closed"})
        if self._reaper_task is not None and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.create_task(self._reap_loop(), name="mcp-runtime-reaper")
        _LOG.info(
            "per-tenant MCP runtime reaper started",
            reap_interval_s=self._reap_interval_s,
            idle_timeout_s=self._idle_timeout_s,
        )

    async def _reap_loop(self) -> None:
        """Sleep, reap, repeat — cancelled by :meth:`aclose`; survives single-sweep errors."""
        while True:
            try:
                await asyncio.sleep(self._reap_interval_s)
            except asyncio.CancelledError:
                raise
            if self._closed:
                return
            try:
                await self.reap_idle(now=datetime.now(UTC), idle_timeout_s=self._idle_timeout_s)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the reaper survives one bad sweep
                _LOG.warning(
                    "mcp runtime reaper sweep failed; continuing", exc_type=type(exc).__name__
                )

    async def aclose(self) -> None:
        """Cancel the reaper task (idempotent). Call from the FastAPI lifespan shutdown."""
        if self._closed:
            return
        self._closed = True
        if self._reaper_task is not None and not self._reaper_task.done():
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task

    def _endpoint_url(self, machine_id: str) -> str:
        """The per-tenant ``/mcp`` URL the existing N4 client connects to (acceptance #4).

        Fly 6PN private DNS: a specific Machine is reachable at
        ``{id}.vm.{app}.internal`` from the API app on the same org network.
        """
        return f"http://{machine_id}.vm.{self._app}.internal:{self._port}/mcp"

    async def ensure(
        self,
        *,
        owner_id: str,
        server_id: str,
        image: str,
        secret_env: Mapping[str, str],
    ) -> MCPRuntimeInstance:
        name = derive_machine_name(owner_id, server_id)
        # (1) reserve / re-drive the row (condition 2 lives in the store's ON CONFLICT).
        inst = runtime_store.ensure(
            rls_engine=self._rls, owner_id=owner_id, server_id=server_id, image=image
        )
        # (0) spawn-boundary vetting (N6-D-4, the TOCTOU guard) — fail CLOSED before any Fly
        # interaction. An image de-vetted since assign never spawns/adopts; the row records
        # ``unvetted`` for the not-connected signal (N6-D-6). Never a raise.
        if not self._is_runnable_image(image):
            _LOG.warning("per-tenant MCP runtime refused an unvetted image", image=image)
            runtime_store.mark_state(
                rls_engine=self._rls,
                owner_id=owner_id,
                server_id=server_id,
                state="failed",
                reason="unvetted",
            )
            return MCPRuntimeInstance(
                owner_id=owner_id, server_id=server_id, fly_machine_name=name, state="failed"
            )
        try:
            # (2) reconcile by the deterministic name — ADOPT an orphan, don't re-create
            #     (condition 1: recovers a Machine created before a crashed persist).
            machine = await self._fly.get_machine_by_name(name)
            if machine is None:
                machine = await self._fly.create_machine(name=name, image=image, env=secret_env)
                # --- CRASH WINDOW: a process death here leaves an orphaned Machine the
                #     next ensure adopts by name (the row is still pending). ---
                runtime_store.set_fly_machine_id(
                    rls_engine=self._rls,
                    owner_id=owner_id,
                    server_id=server_id,
                    fly_machine_id=machine.id,
                    fly_app=self._app,
                )
            elif inst.fly_machine_id is None:
                # Adoption recovery: a Machine exists but the row never recorded its id.
                runtime_store.set_fly_machine_id(
                    rls_engine=self._rls,
                    owner_id=owner_id,
                    server_id=server_id,
                    fly_machine_id=machine.id,
                    fly_app=self._app,
                )
            if machine.state != "started":
                await self._fly.start_machine(machine.id)
                await self._fly.wait_started(machine.id)
            endpoint = self._endpoint_url(machine.id)
            runtime_store.mark_running(
                rls_engine=self._rls,
                owner_id=owner_id,
                server_id=server_id,
                endpoint_url=endpoint,
            )
            return MCPRuntimeInstance(
                owner_id=owner_id,
                server_id=server_id,
                fly_machine_name=name,
                fly_machine_id=machine.id,
                endpoint_url=endpoint,
                state="running",
            )
        except MCPRuntimeSubstrateError:
            # Fail-soft (N6-D-6): the persona sees not_connected(fly_outage), never a raise.
            _LOG.warning("per-tenant MCP runtime spawn failed", server_id=server_id, name=name)
            runtime_store.mark_state(
                rls_engine=self._rls,
                owner_id=owner_id,
                server_id=server_id,
                state="failed",
                reason="fly_outage",
            )
            return MCPRuntimeInstance(
                owner_id=owner_id,
                server_id=server_id,
                fly_machine_name=name,
                state="failed",
            )

    async def stop(self, *, owner_id: str, server_id: str) -> None:
        inst = runtime_store.get(rls_engine=self._rls, owner_id=owner_id, server_id=server_id)
        if inst is None:
            return  # never started — no-op (idempotent)
        if inst.fly_machine_id is not None:
            try:
                await self._fly.stop_machine(inst.fly_machine_id)
            except MCPRuntimeSubstrateError:
                # Best-effort teardown — mark stopped regardless so the row reflects intent.
                _LOG.warning("fly stop failed; marking row stopped anyway", server_id=server_id)
        runtime_store.mark_state(
            rls_engine=self._rls,
            owner_id=owner_id,
            server_id=server_id,
            state="stopped",
            reason="stopped",
        )

    async def reap_idle(self, *, now: datetime, idle_timeout_s: float) -> int:
        deadline = now - timedelta(seconds=idle_timeout_s)
        rows = runtime_store.list_idle_running(bypass_engine=self._bypass, deadline=deadline)
        reaped = 0
        for r in rows:
            machine_id = r["fly_machine_id"]
            if machine_id:
                try:
                    await self._fly.stop_machine(str(machine_id))
                except MCPRuntimeSubstrateError:
                    # One bad stop must not abort the sweep (SandboxPool reaper discipline).
                    _LOG.warning("reap: fly stop failed; marking stopped", instance_id=r["id"])
            runtime_store.mark_stopped(bypass_engine=self._bypass, instance_id=str(r["id"]))
            reaped += 1
        if reaped:
            _LOG.info("per-tenant MCP runtime reaped idle instances", count=reaped)
        return reaped
