"""The single validated reschedule door (Spec A8, A8-D-7/D-12).

Every schedule cadence change — from the calendar (T9) or the conversational reschedule
verb (T6) — flows through :func:`reschedule`. It is the one write path:

1. reads the current schedule (the ``old`` side of the audit);
2. applies the new cadence via the CAS-guarded :meth:`ScheduleStore.edit` (the mid-flight
   edit race is handled there, A8-D-7);
3. writes a ``schedule.reschedule`` audit row recording ``old → new`` + the actor +
   provenance (the intent-attributed record A6/A5 read).

**Propose-first is structural (A8-D-12):** the ``actor`` is a mandatory, server-set enum.
A ``persona_proposed`` actor may NOT apply directly — it must go through the proposal flow
(T4); until that lands the door fails closed, so no code path can turn a persona-originated
change into a silent write.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from persona.errors import OriginationForbiddenError
from persona.schedules import (
    RescheduleProposal,
    RescheduleProposalStatus,
    render_human_terms,
)

from persona_api.services import audit_service

if TYPE_CHECKING:
    from datetime import datetime

    from persona.schedules import RecurrenceRule, Schedule
    from sqlalchemy import Engine

    from persona_api.schedules.store import ScheduleStore

__all__ = [
    "RescheduleActor",
    "apply_proposal",
    "propose_reschedule",
    "reschedule",
]


class RescheduleActor(StrEnum):
    """Who originated a reschedule — set server-side, never client-trusted (A8-D-12).

    ``persona_proposed`` is the propose-first gate: it can never apply directly through
    the door, only via the user-confirmed proposal flow (T4).
    """

    USER_VIA_UI = "user_via_ui"  # the calendar time-picker / recurrence builder (T9)
    USER_VIA_CHAT = "user_via_chat"  # the conversational reschedule verb, post-confirm (T6)
    PERSONA_PROPOSED = (
        "persona_proposed"  # a persona-originated proposal (T4 — never a direct apply)
    )


def reschedule(
    store: ScheduleStore,
    engine: Engine,
    *,
    owner_id: str,
    schedule_id: str,
    new_schedule: Schedule,
    actor: RescheduleActor,
    provenance: str,
    now: datetime,
) -> Schedule:
    """Apply a validated, audited, CAS-guarded reschedule through the single door.

    Args:
        store: The owner-scoped schedule store (the CAS-guarded edit lives here).
        engine: The RLS engine for the audit write.
        owner_id: The tenant (the RLS scope).
        schedule_id: The schedule being retimed/reruled.
        new_schedule: The proposed schedule (same ``id``/``owner_id``; the new cadence).
            ``ScheduleStore.edit`` preserves the recurrence anchor + fire budget.
        actor: Who originated the change (server-set; ``persona_proposed`` is refused here).
        provenance: A short free-text note on why (carried into the audit).
        now: The reschedule instant (the frame for the recomputed next fire).

    Returns:
        The post-edit schedule (its ``next_fire_at`` recomputed from the new rule).

    Raises:
        OriginationForbiddenError: ``actor`` is ``PERSONA_PROPOSED`` — a persona change
            must go through the user-confirmed proposal flow, never a direct write
            (A8-D-12; the structural propose-first gate, completed in T4).
        ScheduleNotFoundError / ScheduleConcurrentEditError: from the store.
    """
    if actor is RescheduleActor.PERSONA_PROPOSED:
        # Propose-first is structural: the door refuses a persona-originated direct
        # write. The user-confirmed proposal path is wired in T4; until then this fails
        # closed so no persona change can ever be applied silently.
        raise OriginationForbiddenError(
            "a persona-proposed reschedule must be user-confirmed, never applied directly",
            context={"schedule_id": schedule_id, "actor": actor.value},
        )
    if new_schedule.id != schedule_id or new_schedule.owner_id != owner_id:
        raise OriginationForbiddenError(
            "reschedule target mismatch",
            context={"schedule_id": schedule_id, "proposed_id": new_schedule.id},
        )

    before = store.get(owner_id, schedule_id)
    after = store.edit(new_schedule, now=now)
    audit_service.record(
        engine=engine,
        user_id=owner_id,
        action="schedule.reschedule",
        target=schedule_id,
        metadata={
            "actor": actor.value,
            "provenance": provenance,
            "old": _describe(before),
            "new": _describe(after),
        },
    )
    return after


def propose_reschedule(
    store: ScheduleStore,
    *,
    proposal_id: str,
    owner_id: str,
    schedule_id: str,
    persona_id: str,
    new_recurrence: RecurrenceRule | None,
    new_one_time: datetime | None,
    new_timezone: str,
    reason: str,
    now: datetime,
) -> RescheduleProposal:
    """The ONLY persona-originated path — build a PENDING proposal, never a write (A8-D-12).

    A persona that wants to change a schedule (a quiet-hours collision, repeated failures)
    produces a :class:`RescheduleProposal` here; it is applied only after a user resolves it
    (:func:`resolve_proposal`) to ``confirmed`` and :func:`apply_proposal` runs. This function
    touches NO schedule row — propose-first is structural: there is no code path from a persona
    intent to an applied change that does not pass through a user decision.

    Reads the current schedule only to render the proposed cadence in the user's terms (the
    echo the user confirms). Raises :class:`ScheduleNotFoundError` if the schedule is absent.
    """
    current = store.get(owner_id, schedule_id)  # existence check + tz frame; no write
    timezone = new_timezone or current.timezone
    human_terms = render_human_terms(
        recurrence=new_recurrence, one_time_at=new_one_time, timezone=timezone
    )
    return RescheduleProposal(
        proposal_id=proposal_id,
        owner_id=owner_id,
        schedule_id=schedule_id,
        persona_id=persona_id,
        proposed_recurrence=new_recurrence,
        proposed_one_time=new_one_time,
        proposed_timezone=timezone,
        human_terms=human_terms,
        reason=reason,
        created_at=now,
    )


def apply_proposal(
    store: ScheduleStore,
    engine: Engine,
    proposal: RescheduleProposal,
    *,
    now: datetime,
) -> Schedule:
    """Apply a user-CONFIRMED proposal through the single door (A8-D-12, the structural gate).

    The structural guarantee: this refuses any proposal not ``confirmed`` — and a proposal is
    ``confirmed`` only via :func:`resolve_proposal` with a user ``resolved_by``. So a
    persona-originated change reaches ``ScheduleStore.edit`` ONLY carrying a user's decision
    reference (``resolved_by``, threaded into the audit provenance). Applies via
    :func:`reschedule` with ``actor=USER_VIA_CHAT`` (the user confirmed it in conversation).

    Raises:
        OriginationForbiddenError: the proposal is not ``confirmed`` (pending or rejected) —
            no un-confirmed persona change may apply.
        ScheduleNotFoundError / ScheduleConcurrentEditError: from the store.
    """
    if proposal.status is not RescheduleProposalStatus.CONFIRMED:
        raise OriginationForbiddenError(
            "only a user-confirmed proposal may be applied",
            context={"proposal_id": proposal.proposal_id, "status": proposal.status.value},
        )
    current = store.get(proposal.owner_id, proposal.schedule_id)
    new_schedule = current.model_copy(
        update={
            "recurrence": proposal.proposed_recurrence,
            "one_time_at": proposal.proposed_one_time,
            "timezone": proposal.proposed_timezone,
            "updated_at": now,
        }
    )
    return reschedule(
        store,
        engine,
        owner_id=proposal.owner_id,
        schedule_id=proposal.schedule_id,
        new_schedule=new_schedule,
        actor=RescheduleActor.USER_VIA_CHAT,
        provenance=f"persona proposal {proposal.proposal_id} confirmed by {proposal.resolved_by}",
        now=now,
    )


def _describe(schedule: Schedule) -> str:
    """A human-terms one-liner for the audit's old→new record (no raw RRULE)."""
    return render_human_terms(
        recurrence=schedule.recurrence,
        one_time_at=schedule.one_time_at,
        timezone=schedule.timezone,
    )
