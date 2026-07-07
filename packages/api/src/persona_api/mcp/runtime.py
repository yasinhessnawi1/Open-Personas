"""Per-tenant MCP runtime — the vocabulary + the substrate seam (Spec N6, T1).

N6 runs a user's chosen **image-runtime** MCP server (a Docker image with no
``remote_url`` — e.g. ``mcp/google-flights``) **scoped to that user, isolated**, so
the deferred half of N1 (D-N1-7) works: the shared gateway couldn't vary a per-user
secret (D-N1-5 / N4-D-1a), so a **per-tenant runtime instance** is required.

This module holds the **runtime-agnostic** contract every runtime model plugs into
(N6-D-1 ratified substrate = a per-tenant Fly Machine; the Protocol keeps it
swappable, N6-D-1a). It defines NO substrate — no Fly, no Docker — only:

- :class:`MCPRuntimeState` — the instance lifecycle, matching the
  ``mcp_runtime_instances.state`` CHECK enum (N6-D-7).
- :data:`NotConnectedReason` — the reason vocabulary the not-connected signal
  (R4-C1-21, N6-D-6) surfaces when an assigned server isn't serving tools.
- :class:`MCPServerConnection` — the per-server signal value (badge + reason).
- :class:`MCPRuntimeInstance` — the value :meth:`PerTenantMCPRuntime.ensure`
  returns: where the instance runs + its state (never a secret).
- :class:`PerTenantMCPRuntime` — the substrate Protocol (ensure / stop / reap).

**Secret isolation (N6-D-2):** :meth:`ensure` receives already-resolved secret env
(``secret_env``) that the api-layer resolver decrypted from the Spec-30 Fernet store
in memory; the runtime injects it at Machine spawn. The value never enters this
module's return values, the persona's context, or any log — the D-N1-5 invariant.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

__all__ = [
    "MCPRuntimeInstance",
    "MCPRuntimeState",
    "MCPServerConnection",
    "NotConnectedReason",
    "PerTenantMCPRuntime",
]

#: The instance lifecycle state, 1:1 with the ``mcp_runtime_instances.state`` CHECK
#: enum (N6-D-7). ``pending`` — row reserved / needs a (re)spawn; ``starting`` — Fly
#: create/start in flight; ``running`` — serving on ``endpoint_url``; ``stopped`` —
#: idle-reaped (restartable); ``failed`` — spawn/health failed (re-driven on the next
#: resolve, N6-D-7a condition 2).
MCPRuntimeState = Literal["pending", "starting", "running", "stopped", "failed"]

#: Why an assigned MCP server is not serving tools (the R4-C1-21 signal, N6-D-6). Each
#: value is a stable, user-surfaceable reason — never a raw exception string.
#:
#: - ``not_enabled`` — assigned but never enabled per-tenant (no instance row).
#: - ``runtime_capacity`` — the per-tenant Machine cap is full; the enable was denied
#:   at ASSIGN time (N6-D-3 guard), never a silent spawn.
#: - ``starting`` — the instance is spawning (``pending`` / ``starting``).
#: - ``spawn_failed`` — the Machine failed to start or health-check (``failed``).
#: - ``stopped`` — idle-reaped; will restart on the next resolve.
#: - ``fly_outage`` — the runtime substrate is unreachable (fail-soft).
#: - ``no_key`` — a credential is stored but ``MCP_CREDENTIAL_KEY`` is unset → fail
#:   closed (never run a credentialed image unauthenticated).
#: - ``unvetted`` — the image is not in the runnable allow-list (N6-D-4).
NotConnectedReason = Literal[
    "not_enabled",
    "runtime_capacity",
    "starting",
    "spawn_failed",
    "stopped",
    "fly_outage",
    "no_key",
    "unvetted",
]


class MCPServerConnection(BaseModel):
    """The not-connected signal for one assigned MCP server (N6-D-6, R4-C1-21).

    Makes "assigned" visibly distinct from "working": a persona's assigned
    ``mcp:<server>`` is either ``connected`` (its tools reach the model) or
    ``not_connected(reason)`` (a badge + a stable reason). Runtime-agnostic — the
    same value ships whether the runtime is A/B/C or off entirely.

    Construct via :meth:`connected` / :meth:`not_connected`, never by hand, so the
    connected↔reason invariant can't be violated.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    server_name: str
    connected: bool
    reason: NotConnectedReason | None = None

    @model_validator(mode="after")
    def _reason_iff_not_connected(self) -> MCPServerConnection:
        """A reason is present exactly when not connected (fail-fast on misuse)."""
        if self.connected and self.reason is not None:
            msg = "a connected server must not carry a not-connected reason"
            raise ValueError(msg)
        if not self.connected and self.reason is None:
            msg = "a not-connected server must carry a reason"
            raise ValueError(msg)
        return self

    @classmethod
    def make_connected(cls, server_name: str) -> MCPServerConnection:
        """The server's tools reach the model."""
        return cls(server_name=server_name, connected=True, reason=None)

    @classmethod
    def make_not_connected(
        cls, server_name: str, reason: NotConnectedReason
    ) -> MCPServerConnection:
        """The server is assigned but not serving; ``reason`` says why."""
        return cls(server_name=server_name, connected=False, reason=reason)


class MCPRuntimeInstance(BaseModel):
    """Where a per-tenant instance runs + its state — the :meth:`PerTenantMCPRuntime.ensure` result.

    Mirrors the durable ``mcp_runtime_instances`` row (N6-D-7) the runtime reconciles
    against Fly. **Carries no secret** — the injected credential lives only in the
    Machine's env (N6-D-2); this value is safe to log/return.

    Attributes:
        owner_id: The tenant who owns the instance (RLS scope).
        server_id: The ``user_mcp_servers`` row this instance backs.
        fly_machine_name: The DETERMINISTIC Machine name derived from
            ``(owner_id, server_id)`` — the crash-window idempotency anchor
            (N6-D-7a condition 1): ``ensure`` reconciles/adopts by this name so a
            process death between Fly-create and row-persist never double-spawns.
        fly_machine_id: The Fly-assigned Machine id once created; ``None`` while a row
            is reserved (``pending``) but not yet spawned.
        endpoint_url: The per-tenant streamable-HTTP ``/mcp`` URL the existing N4
            client connects to unchanged (acceptance #4); ``None`` until ``running``.
        state: The lifecycle state (drives the N6-D-6 signal).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_id: str
    server_id: str
    fly_machine_name: str
    fly_machine_id: str | None = None
    endpoint_url: str | None = None
    state: MCPRuntimeState = "pending"

    def to_connection(self, server_name: str) -> MCPServerConnection:
        """Map this instance's lifecycle state → the per-server signal (N6-D-6).

        ``running`` + an endpoint ⇒ connected; every other state ⇒ not-connected with
        the matching reason. A missing instance (never enabled) is handled by the
        signal caller, not here (there is no instance to map).
        """
        if self.state == "running" and self.endpoint_url:
            return MCPServerConnection.make_connected(server_name)
        reason: NotConnectedReason
        if self.state in ("pending", "starting"):
            reason = "starting"
        elif self.state == "stopped":
            reason = "stopped"
        else:  # "failed", or "running" without an endpoint (defensive)
            reason = "spawn_failed"
        return MCPServerConnection.make_not_connected(server_name, reason)


@runtime_checkable
class PerTenantMCPRuntime(Protocol):
    """The substrate seam for running one image-MCP server per (tenant, server).

    N6-D-1 ratifies the substrate as a per-tenant **Fly Machine**; this Protocol keeps
    it swappable (N6-D-1a) so ``runtime_factory`` + the signal depend on the interface,
    never the Fly implementation. Every method is RLS-aware by contract:

    - :meth:`ensure` + the signal read run under the **per-tenant** engine
      (``persona_app``, FORCE-RLS) — a tenant only ever touches its own instance row.
    - :meth:`reap_idle` runs under the **RLS-bypassing** engine (superuser/voice) —
      it MUST scan cross-tenant (N6-D-7a condition 3); under ``persona_app`` FORCE-RLS
      would hide other tenants' idle rows and nothing would be reaped.
    """

    async def ensure(
        self,
        *,
        owner_id: str,
        server_id: str,
        image: str,
        secret_env: Mapping[str, str],
    ) -> MCPRuntimeInstance:
        """Start (or ADOPT) the per-(tenant, server) instance; return its live state.

        Idempotent across BOTH a happy double-resolve AND the spawn crash-window
        (N6-D-7a condition 1): ``ensure`` reconciles against the substrate by the
        deterministic Machine name and **adopts an existing Machine rather than
        re-creating** it. A row in ``stopped`` / ``failed`` transitions back to
        ``pending`` and re-drives the spawn (condition 2) — it never merely bumps the
        last-used timestamp.

        Args:
            owner_id: The tenant (RLS scope + half the deterministic name).
            server_id: The ``user_mcp_servers`` row (the other half of the name).
            image: The vetted image ref to run (N6-D-4 gate is applied by the caller
                at assign AND here at spawn — belt-and-suspenders).
            secret_env: Already-resolved secret env (from the api-layer resolver,
                N6-D-2), injected at Machine spawn. In-memory only; never persisted,
                logged, or returned.

        Returns:
            The live :class:`MCPRuntimeInstance` (endpoint + state), never a secret.
        """
        ...

    async def stop(self, *, owner_id: str, server_id: str) -> None:
        """Stop + release the per-(tenant, server) instance (idempotent).

        Called on unassign / disable / delete. Stopping a never-started or
        already-stopped instance is a no-op (safe to retry).
        """
        ...

    async def reap_idle(self, *, now: datetime, idle_timeout_s: float) -> int:
        """Stop every instance idle longer than ``idle_timeout_s``; return the count.

        CROSS-TENANT (N6-D-7a condition 3): runs under the RLS-bypassing engine so a
        single sweep reaps every tenant's idle instances. Pure-function ``now`` (the
        ``SandboxPool`` reaper precedent) so tests drive time deterministically.
        """
        ...
