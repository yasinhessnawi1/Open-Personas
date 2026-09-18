"""Applying initiative verbs — the api half of the T10 seam (Spec A5).

The chat-turn worker routes ``initiative_verb`` RunEvents here (the same
division as steering/reschedule: the runtime knows the verb, the worker owns
the tenant, this service owns the mutation):

- **dial verbs** — write ``personas.initiative_dial`` (+ ``_updated_at``),
  audit the transition, and ENSURE the persona's scan schedule when the dial
  is not OFF (the lazy provisioning call site, A5-D-1). OFF leaves the
  schedule in place — the handler exit is the one source of truth — and
  records the durable half of a stop verb: the initiatives already in front of
  the user are declined (``stop_verb``), so restraint remembers WHAT was
  stopped and not merely that it was.
- **confirm** — execute the LEDGER-pending proposal through the executor's
  ``execute_confirmed`` (implicit task or the A8 door — the real doors only).
- **decline** — record the durable decline (``declined_reply``, user-level,
  binds all personas — A5-D-4) and supersede the notice slot.

Every application is fail-soft (a verb that cannot apply logs + audits and
never crashes the turn) and audited (no silent state changes).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.initiative import InitiativeDial
from persona.logging import get_logger
from persona_runtime.initiative.verbs import InitiativeVerb

from persona_api.initiative.handler import ensure_initiative_schedule, set_initiative_dial
from persona_api.initiative.store import DeclineSource
from persona_api.services import audit_service

if TYPE_CHECKING:
    from persona.initiative import InitiativeSettings
    from sqlalchemy import Engine

    from persona_api.initiative.delivery import InitiativeDeliveryExecutor
    from persona_api.initiative.store import DeclineStore, InitiativeLedger
    from persona_api.schedules import ScheduleStore
    from persona_api.schedules.tombstones import ScheduleTombstoneStore

__all__ = ["InitiativeVerbService"]

_log = get_logger("api.initiative.verbs")

_DIAL_FOR_VERB: dict[InitiativeVerb, InitiativeDial] = {
    InitiativeVerb.DIAL_OFF: InitiativeDial.OFF,
    InitiativeVerb.DIAL_PROPOSE_ONLY: InitiativeDial.PROPOSE_ONLY,
    InitiativeVerb.DIAL_ACT: InitiativeDial.ACT_WITHIN_ENVELOPE,
}


class InitiativeVerbService:
    """Owner-scoped application of the initiative-verb family."""

    def __init__(
        self,
        *,
        rls_engine: Engine,
        schedules: ScheduleStore,
        ledger: InitiativeLedger,
        declines: DeclineStore,
        executor: InitiativeDeliveryExecutor,
        settings: InitiativeSettings,
        timezone_resolver: object | None = None,
        tombstones: ScheduleTombstoneStore | None = None,
        tombstone_window_days: int = 30,
    ) -> None:
        """Inject the stores + the executor; the tz resolver feeds the ensure.

        ``tombstones`` (R9-037) — threaded into the dial verb's own
        ``ensure_initiative_schedule`` call (the SAME shared function the
        ``InitiativeProvisioner`` sweep calls): a persona whose scan schedule the
        user recently deleted is not silently resurrected by a dial-verb turn
        either — a chat command that changes an unrelated setting (the dial)
        is not a fresh, explicit ask for THIS specific schedule back.
        """
        self._engine = rls_engine
        self._schedules = schedules
        self._ledger = ledger
        self._declines = declines
        self._executor = executor
        self._settings = settings
        self._timezone_resolver = timezone_resolver
        self._tombstones = tombstones
        self._tombstone_window_days = tombstone_window_days

    async def apply(
        self, *, owner_id: str, persona_id: str, verb: str, notice_id: str | None
    ) -> None:
        """Apply one verb event; never raises into the worker (fail-soft + audited)."""
        try:
            parsed = InitiativeVerb(verb)
        except ValueError:
            _log.warning("unknown initiative verb; ignoring verb={verb}", verb=verb)
            return
        try:
            if parsed in _DIAL_FOR_VERB:
                self._apply_dial(owner_id, persona_id, _DIAL_FOR_VERB[parsed])
            elif parsed is InitiativeVerb.CONFIRM_PROPOSAL and notice_id is not None:
                await self._apply_confirm(owner_id, notice_id)
            elif parsed is InitiativeVerb.DECLINE_PROPOSAL and notice_id is not None:
                self._apply_decline(owner_id, persona_id, notice_id)
        except Exception:  # noqa: BLE001 — a verb that cannot apply never crashes the turn
            _log.warning("initiative verb application failed (fail-soft) verb={verb}", verb=verb)

    def _apply_dial(self, owner_id: str, persona_id: str, dial: InitiativeDial) -> None:
        """Write the dial + audit (shared path); ensure the scan schedule when not OFF (A5-D-1)."""
        now = datetime.now(UTC)
        if not set_initiative_dial(self._engine, owner_id, persona_id, dial, now=now):
            _log.warning("dial write matched no persona; ignoring")
            return
        if dial is InitiativeDial.OFF:
            self._record_stop_verb_declines(owner_id, persona_id, now=now)
        else:
            timezone = "UTC"
            if callable(self._timezone_resolver):
                try:
                    timezone = str(self._timezone_resolver(owner_id))
                except Exception:  # noqa: BLE001 — tz lookup must never break the dial write
                    timezone = "UTC"
            ensure_initiative_schedule(
                self._schedules,
                owner_id=owner_id,
                persona_id=persona_id,
                timezone=timezone,
                settings=self._settings,
                now=now,
                tombstones=self._tombstones,
                tombstone_window_days=self._tombstone_window_days,
                seam="initiative_dial_verb",
            )

    def _record_stop_verb_declines(self, owner_id: str, persona_id: str, *, now: datetime) -> None:
        """Record what a STOP verb declines, not only the posture it sets (``stop_verb``).

        "Stop suggesting things" carries two messages and only one of them was
        being kept. The posture half is the dial, which silences this persona
        from here on. The durable half is what the user was reacting to: the
        initiatives this persona actually put in front of them that still own
        their opportunity slot. Those are declined through the SAME store
        method the reply path uses, so restraint learns from a stop verb
        exactly as it learns from a typed "no" — user-level, binding every
        persona until an explicit revival (A5-D-4).

        **The ruling on a GLOBAL stop.** The decline table is keyed by
        opportunity, so a record with no opportunity is not representable, and
        a record per HISTORICAL opportunity would suppress topics the user
        never saw. The truth is one record per DELIVERED, still-live notice
        inside the answerable window (``hold_max_days``, the same window that
        makes a proposal confirmable): the things in front of the user when
        they said stop, and nothing else. A stop verb with nothing delivered
        writes no decline — the dial alone carries it and there is no topic to
        remember. Unlike the silence-driven expiry sweep this covers ``act``
        reports as well as proposals: an explicit stop is the user speaking
        about everything the persona has been doing, not only what it asked.

        The dial write has already committed when this runs, and a decline
        failure must never read as "the dial did not apply" — hence its own
        fail-soft guard.
        """
        try:
            shown = self._ledger.delivered_in_window(
                owner_id,
                persona_id,
                max_age_days=self._settings.hold_max_days,
                now=now,
            )
            for notice in shown:
                self._declines.record_decline(
                    owner_id,
                    opportunity_key=notice.opportunity_key,
                    trigger=notice.trigger,
                    source=DeclineSource.STOP_VERB,
                    persona_id=persona_id,
                    now=now,
                )
                self._ledger.supersede(owner_id, notice.opportunity_key, now=now)
        except Exception:  # noqa: BLE001 — the dial write stands; the ledger retries nothing
            _log.warning("stop-verb decline recording failed (dial write stands)")

    async def _apply_confirm(self, owner_id: str, notice_id: str) -> None:
        executed = await self._executor.execute_confirmed(owner_id, notice_id)
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action="initiative.proposal_confirmed",
            target=notice_id,
            metadata={"executed": str(executed).lower()},
        )

    def _apply_decline(self, owner_id: str, persona_id: str, notice_id: str) -> None:
        notice = self._ledger.get_notice(owner_id, notice_id)
        if notice is None:
            return
        now = datetime.now(UTC)
        self._declines.record_decline(
            owner_id,
            opportunity_key=notice.opportunity_key,
            trigger=notice.trigger,
            source=DeclineSource.DECLINED_REPLY,
            persona_id=persona_id,
            now=now,
        )
        self._ledger.supersede(owner_id, notice.opportunity_key, now=now)
