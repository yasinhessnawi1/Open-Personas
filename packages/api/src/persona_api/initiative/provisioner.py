"""The initiative-schedule provisioning sweep (Spec A5, T10; the T6 gate lean, built).

The population-level built-but-inert killer: when ``PERSONA_INITIATIVE_ENABLED``
flips ON in an environment, PRE-EXISTING personas (dial defaulted propose-only)
would read enabled-but-never-scanning — no schedule row, no fire, forever. The
sweep closes that gap: a **leader-gated, idempotent** worker-loop periodic (the
``CatalogSyncTask`` shape — own advisory-lock key, fresh transient leader per
run) that ensures the per-persona A1 scan schedule for every persona whose dial
is not OFF and that lacks its ``initsched:{persona_id}`` row.

Idempotent three ways: the candidate query is NOT-EXISTS (already-provisioned
personas never match), the ensure is get-or-create on the deterministic id, and
a create race converges on the PK. Lazy provisioning (persona-create + dial
write) remains the immediate path; the sweep is the flag-flip/backfill floor.
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.timezone import resolve_timezone
from sqlalchemy import text

from persona_api.initiative.handler import ensure_initiative_schedule, initiative_schedule_id
from persona_api.schedules.leadership import SchedulerLeader

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from persona.initiative import InitiativeSettings
    from sqlalchemy import Engine

    from persona_api.jobs.catalog_sync import LeaderGate
    from persona_api.schedules import ScheduleStore
    from persona_api.schedules.tombstones import ScheduleTombstoneStore

__all__ = ["INITIATIVE_PROVISIONER_LOCK_KEY", "InitiativeProvisioner"]

_log = get_logger("api.initiative.provisioner")

#: Distinct advisory-lock key (independent of tick/catalog leadership).
INITIATIVE_PROVISIONER_LOCK_KEY: int = zlib.crc32(b"persona:initiative:provisioner")

_CANDIDATES_SQL = text(
    """
    SELECT p.id AS persona_id, p.owner_id AS owner_id, u.timezone AS timezone
    FROM personas p
    JOIN users u ON u.id = p.owner_id
    WHERE p.initiative_dial != 'off'
      AND NOT EXISTS (
          SELECT 1 FROM schedules s WHERE s.id = ('initsched:' || p.id)
      )
    ORDER BY p.created_at
    LIMIT :batch
    """
)


class InitiativeProvisioner:
    """One leader-gated provisioning sweep (the worker drives it on a cadence)."""

    _BATCH = 200  # per-run bound; the sweep converges over runs (idempotent)

    def __init__(
        self,
        *,
        dispatch_engine: Engine,
        store: ScheduleStore,
        settings: InitiativeSettings,
        default_timezone: str,
        lock_key: int = INITIATIVE_PROVISIONER_LOCK_KEY,
        leader_factory: Callable[[], LeaderGate] | None = None,
        tombstones: ScheduleTombstoneStore | None = None,
        tombstone_window_days: int = 30,
    ) -> None:
        """Inject the cross-tenant read engine + the RLS schedule store + knobs.

        The candidate SELECT runs on the dispatch engine (cross-tenant, like the
        tick's due-claim); each ensure runs owner-scoped through the RLS store.

        ``tombstones`` (R9-037) — when wired, a persona whose scan schedule the
        user recently deleted is SKIPPED (refused, audited), not silently
        re-provisioned; ``None`` preserves the pre-R9-037 unconditional-ensure
        behaviour (tests that don't care).
        """
        self._dispatch_engine = dispatch_engine
        self._store = store
        self._settings = settings
        self._default_timezone = default_timezone
        self._lock_key = lock_key
        self._leader_factory = leader_factory or (
            lambda: SchedulerLeader(self._dispatch_engine, lock_key=self._lock_key)
        )
        self._tombstones = tombstones
        self._tombstone_window_days = tombstone_window_days

    def run_once(self, *, now: datetime) -> int:
        """Provision missing scan schedules; returns how many were ACTUALLY created.

        A non-leader call is a clean no-op (another worker is sweeping). Any
        per-persona failure is logged and skipped — one bad row never blocks
        the population (fail-soft; the next run retries it). A tombstone-refused
        persona (R9-037) is neither counted nor retried noisily — the refusal is
        already audited by :func:`~persona_api.initiative.handler.ensure_initiative_schedule`
        itself; it naturally retries every run until the tombstone window elapses.
        """
        leader = self._leader_factory()
        if not leader.try_become_leader():
            return 0
        try:
            with self._dispatch_engine.begin() as conn:
                rows = conn.execute(_CANDIDATES_SQL, {"batch": self._BATCH}).all()
            provisioned = 0
            refused = 0
            for persona_id, owner_id, stored_tz in rows:
                try:
                    result = ensure_initiative_schedule(
                        self._store,
                        owner_id=str(owner_id),
                        persona_id=str(persona_id),
                        timezone=resolve_timezone(stored_tz, default=self._default_timezone),
                        settings=self._settings,
                        now=now,
                        tombstones=self._tombstones,
                        tombstone_window_days=self._tombstone_window_days,
                        seam="initiative_provisioner",
                    )
                    if result.created:
                        provisioned += 1
                    elif result.refused:
                        refused += 1
                except Exception:  # noqa: BLE001 — one bad row never blocks the sweep
                    _log.warning(
                        "provisioning failed for persona={pid}; will retry next run",
                        pid=str(persona_id),
                    )
            if provisioned:
                _log.info(
                    "initiative provisioner ensured schedules count={count}", count=provisioned
                )
            if refused:
                _log.info(
                    "initiative provisioner refused count={count} (recently user-deleted)",
                    count=refused,
                )
            return provisioned
        finally:
            leader.resign()


def provisioned_schedule_exists(store: ScheduleStore, owner_id: str, persona_id: str) -> bool:
    """A tiny read used by tests/A6: whether the persona's scan schedule exists."""
    from persona.errors import ScheduleNotFoundError

    try:
        store.get(owner_id, initiative_schedule_id(persona_id))
    except ScheduleNotFoundError:
        return False
    return True
