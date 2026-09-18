"""The ignored-proposal expiry sweep (Spec A5, T3; the third decline source, wired).

Silence is the most common way a person declines an unprompted message, and it
was the one way the restraint ledger could not see. A proposal the user simply
never answers drops out of the confirmable window and, until this sweep existed,
left no trace at all: the persona would keep proposing the same shape of thing
to someone who had ignored it five times, and back off only for the people who
took the trouble to reply "no". That is the wrong asymmetry for a feature whose
whole safety argument is restraint.

This is the :class:`~persona_api.initiative.provisioner.InitiativeProvisioner`
shape: a leader-gated, idempotent worker-loop periodic on its own advisory-lock
key, cross-tenant SELECT on the dispatch engine, each write owner-scoped through
the RLS stores. At the expiry transition it does what the reply path does — a
``DeclineRecord`` (``ignored_expiry``) plus the notice supersede that frees the
opportunity slot.

Idempotent two ways, so a re-run can never double-count silence: the supersede
takes the row out of the candidate query, and the decline's partial unique
converges a duplicate to a no-op even if the supersede failed in between.

Scope is deliberately narrow: only ``propose`` notices. Silence answers a
question, and an act-then-report asked none — reading a shrug at a report as a
durable, all-persona decline would be the same over-reach in the other
direction. An explicit stop verb DOES cover both (see
``InitiativeVerbService._record_stop_verb_declines``): the user speaking is
broad, the user's silence is conservative.
"""

from __future__ import annotations

import zlib
from datetime import timedelta
from typing import TYPE_CHECKING

from persona.logging import get_logger
from sqlalchemy import text

from persona_api.initiative.store import DeclineSource
from persona_api.schedules.leadership import SchedulerLeader

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from persona.initiative import InitiativeSettings
    from sqlalchemy import Engine

    from persona_api.initiative.store import DeclineStore, InitiativeLedger
    from persona_api.jobs.catalog_sync import LeaderGate

__all__ = ["IGNORED_PROPOSAL_SWEEP_LOCK_KEY", "IgnoredProposalSweeper"]

_log = get_logger("api.initiative.ignored_sweep")

#: Distinct advisory-lock key (independent of tick/catalog/provisioner leadership).
IGNORED_PROPOSAL_SWEEP_LOCK_KEY: int = zlib.crc32(b"persona:initiative:ignored-proposals")

_EXPIRED_SQL = text(
    """
    SELECT id, owner_id, persona_id, opportunity_key, trigger
    FROM initiative_notices
    WHERE disposition = 'delivered'
      AND envelope_action = 'propose'
      AND superseded_at IS NULL
      AND delivered_at IS NOT NULL
      AND delivered_at < :cutoff
    ORDER BY delivered_at
    LIMIT :batch
    """
)


class IgnoredProposalSweeper:
    """One leader-gated sweep that turns an unanswered proposal into a decline."""

    _BATCH = 200  # per-run bound; the sweep converges over runs (idempotent)

    def __init__(
        self,
        *,
        dispatch_engine: Engine,
        declines: DeclineStore,
        ledger: InitiativeLedger,
        settings: InitiativeSettings,
        lock_key: int = IGNORED_PROPOSAL_SWEEP_LOCK_KEY,
        leader_factory: Callable[[], LeaderGate] | None = None,
    ) -> None:
        """Inject the cross-tenant read engine, the RLS stores and the window knob.

        The answerable window is ``settings.hold_max_days`` — the same window
        that decides whether a delivered proposal is still confirmable, so a
        proposal becomes ignorable at exactly the moment it stops being
        answerable. No second knob, no second definition of "too late".
        """
        self._dispatch_engine = dispatch_engine
        self._declines = declines
        self._ledger = ledger
        self._settings = settings
        self._lock_key = lock_key
        self._leader_factory = leader_factory or (
            lambda: SchedulerLeader(self._dispatch_engine, lock_key=self._lock_key)
        )

    def run_once(self, *, now: datetime) -> int:
        """Decline every proposal that aged out unanswered; returns records WRITTEN.

        A non-leader call is a clean no-op (another worker is sweeping). Any
        per-notice failure is logged and skipped — one bad row never blocks the
        population, and the next run retries it (fail-soft). A notice whose
        topic is already live-declined counts zero: the store's partial unique
        absorbs it, which is what makes a re-run harmless.
        """
        leader = self._leader_factory()
        if not leader.try_become_leader():
            return 0
        try:
            cutoff = now - timedelta(days=self._settings.hold_max_days)
            with self._dispatch_engine.begin() as conn:
                rows = conn.execute(_EXPIRED_SQL, {"cutoff": cutoff, "batch": self._BATCH}).all()
            declined = 0
            for _notice_id, owner_id, persona_id, opportunity_key, trigger in rows:
                try:
                    record = self._declines.record_decline(
                        str(owner_id),
                        opportunity_key=str(opportunity_key),
                        trigger=str(trigger),
                        source=DeclineSource.IGNORED_EXPIRY,
                        persona_id=str(persona_id),
                        now=now,
                    )
                    # Free the slot exactly as the reply path does: the decline is
                    # now the live suppressor, and a revival re-opens the topic.
                    self._ledger.supersede(str(owner_id), str(opportunity_key), now=now)
                except Exception:  # noqa: BLE001 — one bad row never blocks the sweep
                    _log.warning(
                        "ignored-proposal decline failed for key={key}; will retry next run",
                        key=str(opportunity_key),
                    )
                    continue
                if record is not None:
                    declined += 1
            if declined:
                _log.info("ignored proposals declined count={count}", count=declined)
            return declined
        finally:
            leader.resign()
