"""The skill-catalog auto-sync task — a leader-gated worker-loop periodic (Spec S2, C1).

S2 keeps the external skill mirror fresh automatically, reusing N2's leader-gated substrate
(S2-D-1) — a fourth ownerless periodic alongside the MCP catalog sync, the scheduler tick, and
maintenance. :class:`SkillCatalogSyncTask` is the unit of one sync:

1. **Leader-gate** (reuse) — acquire a Postgres advisory lock with a **distinct** key
   (``crc32(b"persona:skill-catalog:leader")``, S2-D-X-leader-key) so skill-sync leadership is
   independent of MCP-catalog leadership. Acquired + released within one ``run_once`` (transient),
   so the held DBAPI connection never crosses calls/threads (the to_thread offload is thread-safe).
2. **Sync** — clone the enabled curated sources, ingest + tier them, and reconcile into the mirror
   (:func:`~persona.skills.skill_sources_sync.sync_skill_mirror`). The Anthropic ingest verifies the
   pinned canonical coordinate before stamping ``vetted`` (S2-D-4); a vetted-authenticity failure
   raises and is caught by the fail-soft envelope below.
3. **Observe** — log ran-at + added/updated/removed counts (structured-log posture; an ownerless
   event doesn't fit the owner-scoped ``audit_log``).

**Fail-soft:** a clone/parse/authenticity failure raises BEFORE the atomic write, so the last-good
mirror is preserved (D-N1-4); ``run_once`` catches it, logs, and returns ``None`` — the worker
retries on the next cadence. A non-leader call is a clean no-op.

**Blocking-aware:** the clone + file I/O is blocking; the worker loop calls ``run_once`` via
``asyncio.to_thread`` so it never stalls the shared event loop.

**Availability ≠ enablement:** a sync makes a skill *available* in the mirror; it NEVER
auto-enables it on any persona (the runtime loads only skills a persona *declared*, and gates
injection per tier).
"""

from __future__ import annotations

import tempfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from persona.skills.skill_mirror import resolve_skill_mirror_write_path
from persona.skills.skill_mirror_reconcile import SkillMirrorSyncResult
from persona.skills.skill_sources_sync import clone_at_ref, sync_skill_mirror
from persona.skills.sources.anthropic import ANTHROPIC_PINNED_COMMIT, ANTHROPIC_REPO_URL

from persona_api.schedules.leadership import SchedulerLeader

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = [
    "SKILL_CATALOG_SYNC_LEADER_LOCK_KEY",
    "LeaderGate",
    "SkillCatalogSyncTask",
    "build_skill_catalog_sync",
]


@runtime_checkable
class LeaderGate(Protocol):
    """The leadership primitive the sync needs: acquire-or-confirm, then release."""

    def try_become_leader(self) -> bool: ...

    def resign(self) -> None: ...


_log = get_logger("api.jobs.skill_catalog_sync")

#: The advisory-lock key for skill-catalog-sync leadership (S2-D-X-leader-key). Distinct from
#: the MCP catalog-sync key so the two leaderships are orthogonal — ``zlib.crc32`` is
#: deterministic across processes (every worker computes the same key).
SKILL_CATALOG_SYNC_LEADER_LOCK_KEY: int = zlib.crc32(b"persona:skill-catalog:leader")


class SkillCatalogSyncTask:
    """One leader-gated skill-mirror reconcile (the worker drives it on a cadence).

    Args:
        dispatch_engine: The cross-tenant engine the advisory lock is taken on (advisory
            locks are not tenant data — no RLS, like the scheduler leader).
        mirror_path: The writable snapshot path the reconcile writes.
        openclaw_repo_url: The curated OpenClaw source repo URL (``None`` ⇒ OpenClaw disabled).
        openclaw_ref: The OpenClaw source ref to pin (``None`` ⇒ default branch HEAD).
        lock_key: The skill-catalog-sync advisory-lock key (defaults to the module key).
        leader_factory: Test seam — builds the per-run leader (default ``SchedulerLeader``).
        sync_runner: Test seam — runs the clone+sync (default clones the curated sources).
    """

    def __init__(
        self,
        *,
        dispatch_engine: Engine,
        mirror_path: Path = Path("/var/lib/persona/skill_mirror/skill_mirror.json"),
        openclaw_repo_url: str | None = None,
        openclaw_ref: str | None = None,
        lock_key: int = SKILL_CATALOG_SYNC_LEADER_LOCK_KEY,
        leader_factory: Callable[[], LeaderGate] | None = None,
        sync_runner: Callable[[], SkillMirrorSyncResult] | None = None,
    ) -> None:
        self._dispatch_engine = dispatch_engine
        self._mirror_path = mirror_path
        self._openclaw_repo_url = openclaw_repo_url
        self._openclaw_ref = openclaw_ref
        self._lock_key = lock_key
        self._leader_factory: Callable[[], LeaderGate] = leader_factory or (
            lambda: SchedulerLeader(dispatch_engine, lock_key=lock_key)
        )
        self._sync_runner: Callable[[], SkillMirrorSyncResult] = sync_runner or self._clone_and_sync

    def run_once(self) -> SkillMirrorSyncResult | None:
        """Run one sync if this process wins the leader lock; else a clean no-op.

        BLOCKING (git clone + file I/O) — the worker loop calls this via ``asyncio.to_thread``.
        Returns the result on a completed sync, or ``None`` when not leader or when the sync
        failed (fail-soft: the last-good mirror is preserved by the build raising before write).
        """
        leader = self._leader_factory()
        if not leader.try_become_leader():
            _log.debug("skill catalog sync skipped: not leader", lock_key=self._lock_key)
            leader.resign()
            return None
        try:
            result = self._sync_runner()
        except Exception:  # noqa: BLE001 — fail-soft: last-good mirror intact, retry next cadence
            _log.exception(
                "skill catalog sync failed; keeping last-good mirror", path=str(self._mirror_path)
            )
            return None
        finally:
            leader.resign()
        _log.info(
            "skill catalog sync completed",
            ran_at=datetime.now(UTC).isoformat(),
            added=len(result.added),
            updated=len(result.updated),
            removed=len(result.removed),
            total=result.total,
            path=str(self._mirror_path),
        )
        return result

    def _clone_and_sync(self) -> SkillMirrorSyncResult:
        """Clone the enabled curated sources into temp dirs, ingest+tier, reconcile (OFFLINE)."""
        with tempfile.TemporaryDirectory(prefix="persona-skill-sync-") as tmp:
            tmp_root = Path(tmp)
            anthropic = clone_at_ref(
                ANTHROPIC_REPO_URL, ANTHROPIC_PINNED_COMMIT, tmp_root / "anthropic"
            )
            openclaw = (
                clone_at_ref(self._openclaw_repo_url, self._openclaw_ref, tmp_root / "openclaw")
                if self._openclaw_repo_url is not None
                else None
            )
            return sync_skill_mirror(
                mirror_path=self._mirror_path, anthropic=anthropic, openclaw=openclaw
            )


def build_skill_catalog_sync(
    config: APIConfig, *, dispatch_engine: Engine
) -> SkillCatalogSyncTask | None:
    """Compose the skill-catalog-sync task from config, or ``None`` when disabled.

    Returns ``None`` when ``skill_catalog_sync_enabled`` is off (the opt-out — availability then
    stays at the last-synced snapshot, fail-soft), or when no writable mirror path is configured
    (``PERSONA_SKILL_MIRROR_PATH`` unset): the bundled package-data snapshot is a read-time
    fallback ONLY (N2-D-1 posture), never a write target — writing it would mutate committed
    package data in a checkout (R9-011) and fails on a deployed image anyway (root-owned
    ``/app``). Otherwise builds the task on the worker's dispatch engine.
    """
    if not config.skill_catalog_sync_enabled:
        _log.info("skill catalog auto-sync disabled (PERSONA_SKILL_SYNC_ENABLED=false)")
        return None
    from persona.config import PersonaCoreConfig

    mirror_path = resolve_skill_mirror_write_path(PersonaCoreConfig().skill_mirror_path)
    if mirror_path is None:
        _log.warning(
            "skill catalog auto-sync skipped: PERSONA_SKILL_MIRROR_PATH unset — the bundled "
            "snapshot is read-only; set an explicit writable path to enable the sync"
        )
        return None
    return SkillCatalogSyncTask(
        dispatch_engine=dispatch_engine,
        mirror_path=mirror_path,
        openclaw_repo_url=config.skill_openclaw_repo_url or None,
        openclaw_ref=config.skill_openclaw_ref or None,
    )
