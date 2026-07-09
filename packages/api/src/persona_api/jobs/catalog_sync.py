"""The MCP catalog auto-sync task — a leader-gated worker-loop periodic (Spec N2, T3).

N2 keeps N1's Docker catalog mirror fresh automatically. A catalog sync is a **system-level
(ownerless)** recurring job, so it does NOT use A1's owner-scoped schedules; instead it is the
identical ownerless-periodic shape as the worker loop's existing ``_maybe_run_maintenance`` /
``_maybe_run_scheduler_tick`` siblings — a third periodic task the worker drives on its own
cadence (N2-D-1).

:class:`CatalogSyncTask` is the unit of one sync:

1. **Leader-gate** (N2-D-2) — acquire a Postgres advisory lock with the catalog-sync key
   (independent of the scheduler-tick leader) so exactly one process pulls. Single-machine today
   makes this moot, but it expresses "one puller" for the network clone and keeps the sync
   coherent with N2-D-1's documented multi-replica escape hatch. The lock is acquired AND released
   **within one** :meth:`run_once` (transient, not held across runs) via a fresh
   :class:`~persona_api.schedules.leadership.SchedulerLeader` — so the whole call is safe to run on
   any thread-pool thread (the held DBAPI connection never crosses calls/threads).
2. **Reconcile** (criteria 1/2/5) — :func:`~persona.tools.mcp.mirror_reconcile.reconcile_mirror`
   re-pulls + diffs + atomically writes the snapshot at the resolved writable path
   (:func:`~persona.tools.mcp.mirror.resolve_mirror_write_path`).
3. **Observe** (criterion 5, N2-D-6) — log ran-at + added/updated/removed counts (structured-log
   posture; an ownerless event doesn't fit the owner-scoped ``audit_log``).

**Fail-soft (N2-D-3):** a clone/parse failure raises inside ``reconcile_mirror`` BEFORE the write,
so the last-good mirror is preserved (D-N1-4); :meth:`run_once` catches it, logs, and returns
``None`` — the worker retries on the next cadence. A non-leader call is a clean no-op (returns
``None``, touches no files).

**Blocking-aware:** the reconcile does a blocking ``git clone`` + file I/O. The in-process worker
shares the API event loop, so the worker loop offloads :meth:`run_once` to a thread
(``asyncio.to_thread``) — never inline — so a multi-second clone never stalls request/SSE handling.
"""

from __future__ import annotations

import zlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from persona.tools.mcp.mirror import resolve_mirror_write_path
from persona.tools.mcp.mirror_reconcile import MirrorSyncResult, reconcile_mirror
from persona.tools.mcp.mirror_sync import REGISTRY_REPO

from persona_api.schedules.leadership import SchedulerLeader

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = [
    "CATALOG_SYNC_LEADER_LOCK_KEY",
    "CatalogSyncTask",
    "LeaderGate",
    "build_catalog_sync",
]


@runtime_checkable
class LeaderGate(Protocol):
    """The leadership primitive the sync needs: acquire-or-confirm, then release.

    Satisfied by :class:`~persona_api.schedules.leadership.SchedulerLeader`; a fake
    implements it for unit tests (so ``run_once`` is testable without Postgres).
    """

    def try_become_leader(self) -> bool: ...

    def resign(self) -> None: ...


_log = get_logger("api.jobs.catalog_sync")

#: The advisory-lock key for catalog-sync leadership (N2-D-2). Distinct from the
#: scheduler-tick key so catalog-sync leadership is independent of scheduler leadership —
#: ``zlib.crc32`` is deterministic across processes (every worker computes the same key).
CATALOG_SYNC_LEADER_LOCK_KEY: int = zlib.crc32(b"persona:catalog:leader")


class CatalogSyncTask:
    """One leader-gated MCP catalog reconcile (the worker drives it on a cadence).

    Args:
        dispatch_engine: The cross-tenant engine the advisory lock is taken on (advisory
            locks are not tenant data — no RLS, like the scheduler leader).
        mirror_path: The writable snapshot path the reconcile writes (the resolved
            ``PERSONA_MCP_MIRROR_PATH`` override, else the bundled default).
        repo: The registry git URL (defaults to N1's ``REGISTRY_REPO``).
        lock_key: The catalog-sync advisory-lock key (defaults to the module key).
    """

    def __init__(
        self,
        *,
        dispatch_engine: Engine,
        mirror_path: Path,
        repo: str = REGISTRY_REPO,
        lock_key: int = CATALOG_SYNC_LEADER_LOCK_KEY,
        leader_factory: Callable[[], LeaderGate] | None = None,
    ) -> None:
        self._dispatch_engine = dispatch_engine
        self._mirror_path = mirror_path
        self._repo = repo
        self._lock_key = lock_key
        # A FRESH leader is built per run (transient acquire→resign within one run_once), so
        # the held DBAPI connection never crosses calls/threads (the to_thread offload is
        # then thread-safe). The factory is a unit-test seam; default builds a SchedulerLeader.
        self._leader_factory: Callable[[], LeaderGate] = leader_factory or (
            lambda: SchedulerLeader(dispatch_engine, lock_key=lock_key)
        )

    def run_once(self) -> MirrorSyncResult | None:
        """Run one sync if this process wins the leader lock; else a clean no-op.

        BLOCKING (git clone + file I/O) — the worker loop calls this via
        ``asyncio.to_thread`` so it never stalls the shared event loop. Returns the
        :class:`MirrorSyncResult` on a completed sync, or ``None`` when not leader or when
        the reconcile failed (fail-soft: the last-good mirror is preserved by
        ``reconcile_mirror`` raising before the write).
        """
        leader = self._leader_factory()
        if not leader.try_become_leader():
            _log.debug("catalog sync skipped: not leader", lock_key=self._lock_key)
            return None
        try:
            result = reconcile_mirror(repo=self._repo, mirror_path=self._mirror_path)
        except Exception:  # noqa: BLE001 — fail-soft: last-good mirror intact, retry next cadence
            _log.exception(
                "catalog sync failed; keeping last-good mirror", path=str(self._mirror_path)
            )
            return None
        finally:
            leader.resign()
        _log.info(
            "catalog sync completed",
            ran_at=datetime.now(UTC).isoformat(),
            added=len(result.added),
            updated=len(result.updated),
            removed=len(result.removed),
            total=result.total,
            path=str(self._mirror_path),
        )
        return result


def build_catalog_sync(config: APIConfig, *, dispatch_engine: Engine) -> CatalogSyncTask | None:
    """Compose the catalog-sync task from config, or ``None`` when disabled (N2-D-3).

    Returns ``None`` when ``mcp_catalog_sync_enabled`` is off (the opt-out — availability then
    stays at the bundled snapshot, fail-soft), or when no writable mirror path is configured
    (``PERSONA_MCP_MIRROR_PATH`` unset): the bundled package-data snapshot is a read-time
    fallback ONLY (N2-D-1), never a write target — writing it would mutate committed package
    data in a checkout (R9-011) and fails on a deployed image anyway (root-owned ``/app``).
    Otherwise builds the task on the worker's cross-tenant dispatch engine.
    """
    if not config.mcp_catalog_sync_enabled:
        _log.info("catalog auto-sync disabled (PERSONA_MCP_SYNC_ENABLED=false)")
        return None
    from persona.config import PersonaCoreConfig

    mirror_path = resolve_mirror_write_path(PersonaCoreConfig().mcp_mirror_path)
    if mirror_path is None:
        _log.warning(
            "catalog auto-sync skipped: PERSONA_MCP_MIRROR_PATH unset — the bundled "
            "snapshot is read-only (N2-D-1); set an explicit writable path to enable the sync"
        )
        return None
    return CatalogSyncTask(dispatch_engine=dispatch_engine, mirror_path=mirror_path)
