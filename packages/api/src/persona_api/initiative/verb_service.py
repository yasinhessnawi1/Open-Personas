"""Applying initiative verbs — the api half of the T10 seam (Spec A5).

The chat-turn worker routes ``initiative_verb`` RunEvents here (the same
division as steering/reschedule: the runtime knows the verb, the worker owns
the tenant, this service owns the mutation):

- **dial verbs** — write ``personas.initiative_dial`` (+ ``_updated_at``),
  audit the transition, and ENSURE the persona's scan schedule when the dial
  is not OFF (the lazy provisioning call site, A5-D-1). OFF leaves the
  schedule in place — the handler exit is the one source of truth.
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
from persona_api.services import audit_service

if TYPE_CHECKING:
    from persona.initiative import InitiativeSettings
    from sqlalchemy import Engine

    from persona_api.initiative.delivery import InitiativeDeliveryExecutor
    from persona_api.initiative.store import DeclineStore, InitiativeLedger
    from persona_api.schedules import ScheduleStore

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
    ) -> None:
        """Inject the stores + the executor; the tz resolver feeds the ensure."""
        self._engine = rls_engine
        self._schedules = schedules
        self._ledger = ledger
        self._declines = declines
        self._executor = executor
        self._settings = settings
        self._timezone_resolver = timezone_resolver

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
        if dial is not InitiativeDial.OFF:
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
            )

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
        from persona_api.initiative.store import DeclineSource

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
