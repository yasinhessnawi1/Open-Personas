"""Persistence for per-tenant MCP runtime instances (Spec N6, N6-D-7 / T2a).

RLS-scoped CRUD over ``mcp_runtime_instances`` — the durable record the per-tenant
Fly runtime (N6-D-1) reconciles against so ``ensure``/reap is idempotent across API
restarts. **No secret ever touches this layer** — the injected credential lives only in
the Machine env (N6-D-2); a row records *where an instance runs and its state*.

Two engine contracts (N6-D-7a condition 3), deliberately split:

- **Per-tenant (``persona_app``, FORCE-RLS):** :func:`ensure`, :func:`set_fly_machine_id`,
  :func:`mark_running`, :func:`mark_state`, :func:`touch`, :func:`get`. A tenant only
  ever touches its own rows (the ``user_isolation`` policy).
- **Cross-tenant (RLS-bypassing superuser/voice engine):** :func:`list_idle_running`,
  :func:`mark_stopped`, :func:`delete`. The idle-reaper MUST scan every tenant's rows;
  under ``persona_app`` FORCE-RLS it would see only the current request's tenant and
  reap nothing. These take the bypass engine and key by the row ``id``.

The idempotency guarantee is the ``UNIQUE(owner_id, server_id)`` key (N6-D-7a condition
1): :func:`ensure` is ``INSERT … ON CONFLICT (owner_id, server_id) DO UPDATE`` so a
concurrent double-resolve or a post-restart re-ensure never creates a second row. Its
``ON CONFLICT`` branch also re-drives a ``stopped``/``failed`` row back to ``pending``
(condition 2) — never merely bumping ``last_used_at`` on a dead instance.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, func, select, update
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.models import mcp_runtime_instances as t
from persona_api.mcp.runtime import MCPRuntimeInstance

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "delete_instance",
    "ensure",
    "get",
    "list_for_owner",
    "list_idle_running",
    "mark_running",
    "mark_state",
    "mark_stopped",
    "set_fly_machine_id",
    "touch",
]

#: Instance states that count as "live" (a running or spawning Machine) — the states a
#: ``stopped``/``failed`` row is re-driven OUT of on the next ``ensure`` (condition 2).
_DEAD_STATES = ("stopped", "failed")


def _to_instance(row: dict[str, Any]) -> MCPRuntimeInstance:
    """Project a DB row to the runtime-agnostic value (secretless by construction)."""
    return MCPRuntimeInstance(
        owner_id=str(row["owner_id"]),
        server_id=str(row["server_id"]),
        # The Machine name is DERIVED, not stored (N6-D-7a) — callers recompute it.
        fly_machine_name="",
        fly_machine_id=row["fly_machine_id"],
        endpoint_url=row["endpoint_url"],
        state=row["state"],
    )


def ensure(*, rls_engine: Engine, owner_id: str, server_id: str, image: str) -> MCPRuntimeInstance:
    """Reserve (or re-drive) the (owner, server) instance row; return its current state.

    Idempotent on ``(owner_id, server_id)`` (N6-D-7a condition 1): a first call inserts a
    ``pending`` row; a repeat call on a LIVE row (``pending``/``starting``/``running``)
    only bumps ``last_used_at`` (and refreshes ``image``); a repeat call on a DEAD row
    (``stopped``/``failed``) **re-drives it to ``pending``** and clears the reason
    (condition 2) so the runtime re-spawns. Runs under the per-tenant ``persona_app``
    engine — the WITH CHECK ties the row to the caller.
    """
    now = datetime.now(UTC)
    insert_stmt = pg_insert(t).values(
        owner_id=owner_id,
        server_id=server_id,
        image=image,
        state="pending",
        created_at=now,
        last_used_at=now,
    )
    redrive = t.c.state.in_(_DEAD_STATES)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["owner_id", "server_id"],
        set_={
            "last_used_at": now,
            "image": insert_stmt.excluded.image,
            "state": case((redrive, "pending"), else_=t.c.state),
            "state_reason": case((redrive, None), else_=t.c.state_reason),
        },
    ).returning(*t.c)
    with rls_engine.begin() as conn:
        row = conn.execute(stmt).mappings().first()
    if row is None:  # pragma: no cover — RLS WITH CHECK would reject, not return None
        msg = "could not reserve runtime instance row"
        raise RuntimeError(msg)
    return _to_instance(dict(row))


def set_fly_machine_id(
    *, rls_engine: Engine, owner_id: str, server_id: str, fly_machine_id: str, fly_app: str
) -> None:
    """Persist the Fly-assigned Machine id + app immediately after create (crash-window).

    The window between Fly-create and this write is exactly what ``ensure``'s
    reconcile-by-derived-name closes (N6-D-7a condition 1): if the process dies here, the
    next ``ensure`` recomputes the name, finds the orphaned Machine, and adopts it. Also
    flips the row to ``starting`` (the Machine exists but isn't serving yet).
    """
    with rls_engine.begin() as conn:
        conn.execute(
            update(t)
            .where(t.c.owner_id == owner_id, t.c.server_id == server_id)
            .values(
                fly_machine_id=fly_machine_id,
                fly_app=fly_app,
                state="starting",
                last_used_at=datetime.now(UTC),
            )
        )


def mark_running(*, rls_engine: Engine, owner_id: str, server_id: str, endpoint_url: str) -> None:
    """Mark the instance ``running`` on ``endpoint_url`` (the N4 client connects here)."""
    with rls_engine.begin() as conn:
        conn.execute(
            update(t)
            .where(t.c.owner_id == owner_id, t.c.server_id == server_id)
            .values(
                state="running",
                endpoint_url=endpoint_url,
                state_reason=None,
                last_used_at=datetime.now(UTC),
            )
        )


def mark_state(
    *, rls_engine: Engine, owner_id: str, server_id: str, state: str, reason: str | None = None
) -> None:
    """Set a non-running lifecycle state (``starting`` / ``failed``) + its reason."""
    with rls_engine.begin() as conn:
        conn.execute(
            update(t)
            .where(t.c.owner_id == owner_id, t.c.server_id == server_id)
            .values(state=state, state_reason=reason, last_used_at=datetime.now(UTC))
        )


def touch(*, rls_engine: Engine, owner_id: str, server_id: str) -> None:
    """Bump ``last_used_at`` on a live instance so the reaper sees it fresh (N6-D-3)."""
    with rls_engine.begin() as conn:
        conn.execute(
            update(t)
            .where(t.c.owner_id == owner_id, t.c.server_id == server_id)
            .values(last_used_at=datetime.now(UTC))
        )


def get(*, rls_engine: Engine, owner_id: str, server_id: str) -> MCPRuntimeInstance | None:
    """Return the (owner, server) instance, or ``None`` if none exists (RLS-scoped)."""
    with rls_engine.begin() as conn:
        row = (
            conn.execute(select(t).where(t.c.owner_id == owner_id, t.c.server_id == server_id))
            .mappings()
            .first()
        )
    return _to_instance(dict(row)) if row is not None else None


def list_for_owner(*, rls_engine: Engine, owner_id: str) -> dict[str, MCPRuntimeInstance]:
    """The caller's instances keyed by ``server_id`` — the not-connected signal read (N6-D-6).

    RLS-scoped (runs under ``persona_app``, like ``ensure`` — NOT the reaper's bypass engine):
    a tenant reads only their own instance state. Drives the ``connected``/``not_connected``
    signal on the API + the UI badge from ONE read; no secret touched (the value is secretless
    by construction).
    """
    with rls_engine.begin() as conn:
        rows = conn.execute(select(t).where(t.c.owner_id == owner_id)).mappings().all()
    return {str(r["server_id"]): _to_instance(dict(r)) for r in rows}


def list_idle_running(*, bypass_engine: Engine, deadline: datetime) -> list[dict[str, Any]]:
    """CROSS-TENANT: running instances idle since before ``deadline`` (N6-D-7a condition 3).

    Runs under the RLS-bypassing engine so ONE reap sweep sees every tenant's idle
    instances. Returns raw dicts (``id``, ``owner_id``, ``server_id``, ``fly_machine_id``,
    ``fly_app``) — the reaper stops each Machine then :func:`mark_stopped` by ``id``.
    """
    with bypass_engine.begin() as conn:
        rows = (
            conn.execute(
                select(t.c.id, t.c.owner_id, t.c.server_id, t.c.fly_machine_id, t.c.fly_app).where(
                    t.c.state == "running", t.c.last_used_at < deadline
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def mark_stopped(*, bypass_engine: Engine, instance_id: str) -> None:
    """CROSS-TENANT: mark a reaped instance ``stopped`` by ``id`` (reaper path).

    Keyed by the primary ``id`` (not owner/server) because the reaper runs under the
    bypass engine and acts on rows across tenants. ``stopped`` is restartable: the next
    ``ensure`` re-drives it to ``pending`` (condition 2). Clears the endpoint (no longer
    reachable) and the Fly id (a fresh spawn gets a new Machine).
    """
    with bypass_engine.begin() as conn:
        conn.execute(
            update(t)
            .where(t.c.id == instance_id)
            .values(
                state="stopped",
                endpoint_url=None,
                fly_machine_id=None,
                state_reason="stopped",
                last_used_at=func.now(),
            )
        )


def delete_instance(*, bypass_engine: Engine, instance_id: str) -> None:
    """CROSS-TENANT: remove an instance row by ``id`` (hard teardown, reaper path)."""
    with bypass_engine.begin() as conn:
        conn.execute(sa_delete(t).where(t.c.id == instance_id))
