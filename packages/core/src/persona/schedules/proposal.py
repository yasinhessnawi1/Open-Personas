"""The persona-proposed reschedule record — propose-first, dual-resolution (Spec A8, A8-D-12).

Propose-first is structural: a persona-originated schedule change NEVER applies as a direct
write. It becomes a :class:`RescheduleProposal` (``pending``) that only a **user decision**
resolves; the applying door refuses anything not ``confirmed`` (see
``persona_api.schedules.reschedule``). This module is the pure, durable-ready record + the
idempotent resolution rule — no store, no I/O.

**Dual-resolution-ready (the A6 seam):** the same record is resolvable from either surface —
the chat confirm now (it rides the A4 ``contract_proposal`` conversation-metadata seam), and
A6's approvals inbox later (persisting this exact shape). :func:`resolve_proposal` is
**first-decision-wins idempotent**: a second decision on an already-resolved proposal is a
no-op, so a chat confirm and an inbox click can race without double-applying. When A6 lands
it stores this value + turns the idempotent resolve into a status compare-and-swap — the
shape does not change, so A6 is not painted into a corner.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from persona.schedules.models import RecurrenceRule  # noqa: TC001 — Pydantic needs runtime access

__all__ = [
    "RescheduleProposal",
    "RescheduleProposalStatus",
    "resolve_proposal",
]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        msg = "naive datetime not allowed; use datetime.now(UTC) or attach a tzinfo"
        raise ValueError(msg)
    return value.astimezone(UTC)


class RescheduleProposalStatus(StrEnum):
    """The proposal lifecycle. ``pending`` awaits a user decision; the rest are terminal."""

    PENDING = "pending"
    CONFIRMED = "confirmed"  # the user said yes — the applying door may now apply it
    REJECTED = "rejected"  # the user said no — never applied


class RescheduleProposal(BaseModel):
    """A persona-originated reschedule awaiting a user decision (A8-D-12, propose-first).

    The proposed cadence is carried as an A1 recurrence XOR one-time + the timezone (the same
    XOR the :class:`~persona.schedules.Schedule` enforces); the applying door builds the new
    schedule from the current row + this cadence, so the anchor/fire-budget preservation still
    runs. ``resolved_by`` is the **confirmation reference** — the user id (or channel) whose
    decision authorised the change; a ``confirmed`` proposal without it is unrepresentable.

    Attributes:
        proposal_id: Durable id (the dual-resolution idempotency key — a chat confirm and an
            A6-inbox click on the same id resolve the same record, first wins).
        owner_id: The tenant (the RLS scope).
        schedule_id: The schedule the change targets.
        persona_id: The persona that proposed it (rendered in the persona-voiced ask).
        proposed_recurrence: The proposed recurring rule (XOR ``proposed_one_time``).
        proposed_one_time: The proposed one-time instant (XOR ``proposed_recurrence``).
        proposed_timezone: The IANA zone the proposed cadence is anchored in.
        human_terms: The proposed cadence in human terms (the echo the user confirms).
        reason: Why the persona proposes it (e.g. a quiet-hours collision) — shown to the user.
        status: The lifecycle state (defaults to ``pending``).
        created_at: When the proposal was made (tz-aware UTC).
        resolved_at: When a user decided (``None`` while pending).
        resolved_by: The user/channel reference that decided (``None`` while pending).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    owner_id: str
    schedule_id: str
    persona_id: str
    proposed_recurrence: RecurrenceRule | None = None
    proposed_one_time: datetime | None = None
    proposed_timezone: str
    human_terms: str
    reason: str
    status: RescheduleProposalStatus = RescheduleProposalStatus.PENDING
    created_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    @field_validator("created_at", "resolved_at", "proposed_one_time", mode="after")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _proposed_xor(self) -> RescheduleProposal:
        if (self.proposed_recurrence is not None) == (self.proposed_one_time is not None):
            msg = "exactly one of proposed_recurrence / proposed_one_time must be set (XOR)"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _resolved_consistency(self) -> RescheduleProposal:
        pending = self.status is RescheduleProposalStatus.PENDING
        has_resolution = self.resolved_by is not None or self.resolved_at is not None
        if pending and has_resolution:
            msg = "a pending proposal carries no resolution"
            raise ValueError(msg)
        if not pending and (self.resolved_by is None or self.resolved_at is None):
            msg = "a resolved proposal must record resolved_by + resolved_at"
            raise ValueError(msg)
        return self


def resolve_proposal(
    proposal: RescheduleProposal,
    *,
    approve: bool,
    resolved_by: str,
    now: datetime,
) -> RescheduleProposal:
    """Resolve a proposal — first-decision-wins idempotent (A8-D-12, dual-resolution).

    A ``pending`` proposal transitions to ``confirmed`` (``approve``) or ``rejected`` with the
    decision's ``resolved_by`` (the confirmation reference) + timestamp. An ALREADY-resolved
    proposal is returned unchanged — the second of two racing decisions (chat vs A6 inbox) is
    a no-op, so the change can never double-apply. ``resolved_by`` is the user/channel that
    authorised it; the applying door requires the resulting ``confirmed`` status.
    """
    if proposal.status is not RescheduleProposalStatus.PENDING:
        return proposal  # already decided — first wins (idempotent)
    new_status = (
        RescheduleProposalStatus.CONFIRMED if approve else RescheduleProposalStatus.REJECTED
    )
    return proposal.model_copy(
        update={"status": new_status, "resolved_by": resolved_by, "resolved_at": _ensure_utc(now)}
    )
