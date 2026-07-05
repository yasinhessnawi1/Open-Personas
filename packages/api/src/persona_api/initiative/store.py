"""The initiative restraint stores (Spec A5, T3; A5-D-3/D-4/D-6 + Phase-1 ruling 4).

Two RLS-scoped, audited stores over the ``0NN_initiative`` tables:

- :class:`DeclineStore` — dismissal as durable user communication. A LIVE
  decline suppresses its opportunity for ALL personas (A5-D-4: user-level,
  never per-persona) until an EXPLICIT revival — never a timer. Revival is an
  UPDATE (``revived_at``), never a DELETE: the un-suppression is audit-honest.
- :class:`InitiativeLedger` — the user-level opportunity ledger. Its partial
  unique (one LIVE row per ``(owner, opportunity_key)``) is what makes the
  multi-persona scan race harmless: arbitration (A5-D-6) picks the voicer
  BEFORE the insert, and the loser's claim simply no-ops. It is also the
  cadence-counting substrate (counted at DELIVERY over trailing windows), the
  batch-hold buffer (flush-on-next-scan, Phase-1 ruling 4), and the
  disposition audit A6 later renders — including ``suppressed_stale`` (the T3
  gate ruling 3: a lapsed held candidate is EXPIRED with a written disposition
  + audit row, never silently dropped, never delivered stale).

Discipline (the ScheduleStore template): CQS — reads never write, mutators
return the post-mutation record as confirmation; every mutation emits exactly
one ``audit_log`` row; every operation runs inside the owner's RLS GUC via
:func:`~persona_api.db.engine.rls_connection`, so a cross-tenant reach hits
zero rows. Cadence caps use TRAILING windows (24h / 7d ending now), not
calendar days: no midnight boundary burst and no timezone question in the
counting path (the R7-D-2 2×-burst residual, avoided here by construction).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import uuid4

from persona.initiative import InitiativeCandidate
from persona.initiative.restraint import CadenceCounts
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from persona_api.db.engine import rls_connection
from persona_api.db.models import initiative_declines as declines_t
from persona_api.db.models import initiative_notices as notices_t
from persona_api.services import audit_service

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine, RowMapping

__all__ = [
    "DeclineRecord",
    "DeclineSource",
    "DeclineStore",
    "InitiativeLedger",
    "NoticeDisposition",
    "NoticeRecord",
]

_log = get_logger("api.initiative.store")


class DeclineSource(StrEnum):
    """How a decline was expressed (app-layer gate over the TEXT column)."""

    DECLINED_REPLY = "declined_reply"
    STOP_VERB = "stop_verb"
    IGNORED_EXPIRY = "ignored_expiry"


class NoticeDisposition(StrEnum):
    """A notice row's lifecycle state (app-layer gate over the TEXT column)."""

    HELD = "held"
    DELIVERED = "delivered"
    SUPPRESSED_STALE = "suppressed_stale"


class DeclineRecord(BaseModel):
    """A decline row (frozen read/confirmation shape)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    owner_id: str
    persona_id: str | None
    opportunity_key: str
    trigger: str
    source: DeclineSource
    declined_at: datetime
    revived_at: datetime | None


class NoticeRecord(BaseModel):
    """A ledger row (frozen read/confirmation shape). ``candidate`` is the snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    owner_id: str
    persona_id: str
    opportunity_key: str
    trigger: str
    source: str
    disposition: NoticeDisposition
    envelope_action: str | None
    candidate: InitiativeCandidate
    held_until: datetime | None
    delivered_at: datetime | None
    superseded_at: datetime | None
    created_at: datetime


def _notice_from_row(row: RowMapping) -> NoticeRecord:
    return NoticeRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        persona_id=row["persona_id"],
        opportunity_key=row["opportunity_key"],
        trigger=row["trigger"],
        source=row["source"],
        disposition=NoticeDisposition(row["disposition"]),
        envelope_action=row["envelope_action"],
        candidate=InitiativeCandidate.model_validate(row["candidate"]),
        held_until=row["held_until"],
        delivered_at=row["delivered_at"],
        superseded_at=row["superseded_at"],
        created_at=row["created_at"],
    )


class DeclineStore:
    """Owner-scoped, audited decline records (A5-D-4).

    Construct with the ``persona_app`` RLS engine.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- reads (CQS: no writes) --------------------------------------------

    def suppressed_keys(self, owner_id: str, keys: Sequence[str]) -> set[str]:
        """The subset of ``keys`` with a LIVE decline (the scan-batch suppression check)."""
        if not keys:
            return set()
        with rls_connection(self._engine, owner_id) as conn:
            rows = conn.execute(
                select(declines_t.c.opportunity_key).where(
                    declines_t.c.opportunity_key.in_(list(keys)),
                    declines_t.c.revived_at.is_(None),
                )
            ).all()
        return {r[0] for r in rows}

    def is_suppressed(self, owner_id: str, opportunity_key: str) -> bool:
        """Whether one opportunity has a LIVE decline."""
        return bool(self.suppressed_keys(owner_id, [opportunity_key]))

    # --- mutations (CQS: return confirmation) -------------------------------

    def record_decline(
        self,
        owner_id: str,
        *,
        opportunity_key: str,
        trigger: str,
        source: DeclineSource,
        persona_id: str | None,
        now: datetime,
    ) -> DeclineRecord | None:
        """Record a decline; idempotent against an existing LIVE decline.

        Returns the new record, or ``None`` when the topic is ALREADY live-declined
        (the partial unique conflicts — already suppressed, nothing to add).
        Audits ``initiative.decline`` on a real write only.
        """
        record = DeclineRecord(
            id=f"idecl_{uuid4().hex}",
            owner_id=owner_id,
            persona_id=persona_id,
            opportunity_key=opportunity_key,
            trigger=trigger,
            source=source,
            declined_at=now,
            revived_at=None,
        )
        try:
            with rls_connection(self._engine, owner_id) as conn:
                conn.execute(
                    insert(declines_t).values(
                        id=record.id,
                        owner_id=owner_id,
                        persona_id=persona_id,
                        opportunity_key=opportunity_key,
                        trigger=trigger,
                        source=source.value,
                        declined_at=now,
                        revived_at=None,
                        created_at=now,
                    )
                )
        except IntegrityError:
            # A LIVE decline already suppresses this topic — the retry/duplicate
            # converges (portable across Postgres + community SQLite, no dialect
            # ON CONFLICT needed). GUARDED INVARIANT (T3 gate ruling 1): this broad
            # catch is safe ONLY because owner_id comes from an authenticated
            # request/job — the users row exists before any store call, so the
            # only reachable IntegrityError is the partial-unique conflict. If a
            # future caller could reach this store with an UNSEEDED owner, narrow
            # this catch to the unique-constraint violation (do not let an FK
            # error read as "already declined").
            return None
        self._audit(
            owner_id,
            "initiative.decline",
            opportunity_key,
            extra={"source": source.value, "trigger": trigger},
        )
        return record

    def revive(self, owner_id: str, opportunity_key: str, *, now: datetime) -> bool:
        """Revive a declined topic — the EXPLICIT user act (never a timer).

        An UPDATE, never a DELETE (audit-honest history). Returns whether a live
        decline existed. Audits ``initiative.revive`` on a real revival.
        """
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(
                update(declines_t)
                .where(
                    declines_t.c.opportunity_key == opportunity_key,
                    declines_t.c.revived_at.is_(None),
                )
                .values(revived_at=now)
            )
        revived = result.rowcount > 0
        if revived:
            self._audit(owner_id, "initiative.revive", opportunity_key)
        return revived

    def _audit(
        self, owner_id: str, action: str, target: str, *, extra: dict[str, str] | None = None
    ) -> None:
        audit_service.record(
            engine=self._engine, user_id=owner_id, action=action, target=target, metadata=extra
        )


class InitiativeLedger:
    """The owner-scoped, audited opportunity ledger (A5-D-3/D-6 + ruling 4).

    Construct with the ``persona_app`` RLS engine.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- reads (CQS: no writes) --------------------------------------------

    def get_notice(self, owner_id: str, notice_id: str) -> NoticeRecord | None:
        """One notice by id (RLS-scoped; the delivery executor's canonical read)."""
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(select(notices_t).where(notices_t.c.id == notice_id))
                .mappings()
                .first()
            )
        return None if row is None else _notice_from_row(row)

    def latest_pending_proposal(
        self, owner_id: str, persona_id: str, *, max_age_days: int
    ) -> NoticeRecord | None:
        """The persona's latest confirmable proposal (the T10 verb gate's LEDGER read).

        A DELIVERED ``propose`` notice, not superseded, younger than the
        confirmable window — the reload-durable pending state the confirm/decline
        verbs resolve against (never conversation metadata; the T9 Option-C
        consolidation). RLS-scoped; a read (CQS).
        """
        from datetime import timedelta

        cutoff_interval = timedelta(days=max_age_days)
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(
                    select(notices_t)
                    .where(
                        notices_t.c.persona_id == persona_id,
                        notices_t.c.disposition == NoticeDisposition.DELIVERED.value,
                        notices_t.c.envelope_action == "propose",
                        notices_t.c.superseded_at.is_(None),
                        notices_t.c.delivered_at.isnot(None),
                    )
                    .order_by(notices_t.c.delivered_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        record = _notice_from_row(row)
        if record.delivered_at is None:
            return None
        from datetime import UTC, datetime

        if datetime.now(UTC) - record.delivered_at > cutoff_interval:
            return None  # expired — a stale proposal is not confirmable (bar 2's negative)
        return record

    def held_for_owner(self, owner_id: str) -> list[NoticeRecord]:
        """The owner's held notices, oldest first (the flush read)."""
        with rls_connection(self._engine, owner_id) as conn:
            rows = (
                conn.execute(
                    select(notices_t)
                    .where(
                        notices_t.c.disposition == NoticeDisposition.HELD.value,
                        notices_t.c.superseded_at.is_(None),
                    )
                    .order_by(notices_t.c.created_at.asc())
                )
                .mappings()
                .all()
            )
        return [_notice_from_row(r) for r in rows]

    def delivered_counts(self, owner_id: str, *, persona_id: str, now: datetime) -> CadenceCounts:
        """The A5-D-3 cap inputs, counted at DELIVERY over TRAILING windows.

        Trailing 24h (persona-day + user-day) and 168h (persona-week) windows
        ending ``now`` — no midnight boundary burst, no timezone question.
        """
        day_ago = now - timedelta(hours=24)
        week_ago = now - timedelta(days=7)
        delivered = notices_t.c.disposition == NoticeDisposition.DELIVERED.value
        with rls_connection(self._engine, owner_id) as conn:
            persona_day = conn.execute(
                select(func.count()).where(
                    delivered,
                    notices_t.c.persona_id == persona_id,
                    notices_t.c.delivered_at > day_ago,
                )
            ).scalar_one()
            persona_week = conn.execute(
                select(func.count()).where(
                    delivered,
                    notices_t.c.persona_id == persona_id,
                    notices_t.c.delivered_at > week_ago,
                )
            ).scalar_one()
            user_day = conn.execute(
                select(func.count()).where(delivered, notices_t.c.delivered_at > day_ago)
            ).scalar_one()
        return CadenceCounts(persona_day=persona_day, persona_week=persona_week, user_day=user_day)

    # --- mutations (CQS: return confirmation) -------------------------------

    def try_claim(
        self,
        candidate: InitiativeCandidate,
        *,
        voicer_persona_id: str,
        disposition: NoticeDisposition,
        envelope_action: str | None,
        held_until: datetime | None,
        now: datetime,
    ) -> NoticeRecord | None:
        """Claim the opportunity slot for this candidate (the A5-D-6 arbitration write).

        ``INSERT`` against the partial unique: ``None`` means another persona (or an
        earlier scan) already owns a LIVE notice for this opportunity — the loser
        no-ops, audited ``initiative.duplicate_suppressed``. One user-level notice
        per opportunity is the invariant, regardless of scan ordering or races.
        """
        record = NoticeRecord(
            id=f"intc_{uuid4().hex}",
            owner_id=candidate.owner_id,
            persona_id=voicer_persona_id,
            opportunity_key=candidate.opportunity_key,
            trigger=candidate.trigger.value,
            source=candidate.source.value,
            disposition=disposition,
            envelope_action=envelope_action,
            candidate=candidate,
            held_until=held_until,
            delivered_at=now if disposition is NoticeDisposition.DELIVERED else None,
            superseded_at=None,
            created_at=now,
        )
        try:
            with rls_connection(self._engine, candidate.owner_id) as conn:
                conn.execute(
                    insert(notices_t).values(
                        id=record.id,
                        owner_id=record.owner_id,
                        persona_id=record.persona_id,
                        opportunity_key=record.opportunity_key,
                        trigger=record.trigger,
                        source=record.source,
                        disposition=record.disposition.value,
                        envelope_action=record.envelope_action,
                        candidate=candidate.model_dump(mode="json"),
                        held_until=record.held_until,
                        delivered_at=record.delivered_at,
                        superseded_at=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
        except IntegrityError:
            # GUARDED INVARIANT (T3 gate ruling 1): owners are seeded before any
            # store call in every real path (authenticated request / owner-scoped
            # job), so the only reachable IntegrityError here is the LIVE partial
            # unique — the arbitration loser. If an unseeded-owner path ever
            # appears, narrow this to the unique-constraint violation.
            self._audit(
                candidate.owner_id,
                "initiative.duplicate_suppressed",
                candidate.opportunity_key,
                extra={"loser_persona_id": voicer_persona_id},
            )
            return None
        self._audit(
            candidate.owner_id,
            f"initiative.{disposition.value}",
            candidate.opportunity_key,
            extra={"persona_id": voicer_persona_id, "trigger": record.trigger},
        )
        return record

    def mark_delivered(
        self, owner_id: str, notice_id: str, *, envelope_action: str, now: datetime
    ) -> bool:
        """Promote a held notice to delivered (the flush release). Audited."""
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(
                update(notices_t)
                .where(
                    notices_t.c.id == notice_id,
                    notices_t.c.disposition == NoticeDisposition.HELD.value,
                )
                .values(
                    disposition=NoticeDisposition.DELIVERED.value,
                    envelope_action=envelope_action,
                    delivered_at=now,
                    updated_at=now,
                )
            )
        delivered = result.rowcount > 0
        if delivered:
            self._audit(owner_id, "initiative.delivered", notice_id)
        return delivered

    def expire_stale(self, owner_id: str, notice_id: str, *, reason: str, now: datetime) -> bool:
        """Expire a held notice whose why-now lapsed or grounding dissolved (T3 ruling 3).

        WRITES the ``suppressed_stale`` disposition AND the audit row — the hold
        is a buffer, not a queue that serves rotten items; the expiry is a
        recorded event, never a silent drop.
        """
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(
                update(notices_t)
                .where(
                    notices_t.c.id == notice_id,
                    notices_t.c.disposition == NoticeDisposition.HELD.value,
                )
                .values(
                    disposition=NoticeDisposition.SUPPRESSED_STALE.value,
                    updated_at=now,
                )
            )
        expired = result.rowcount > 0
        if expired:
            self._audit(
                owner_id, "initiative.suppressed_stale", notice_id, extra={"reason": reason}
            )
        return expired

    def supersede(self, owner_id: str, opportunity_key: str, *, now: datetime) -> bool:
        """Free the opportunity slot (paired with a decline revival — A5-D-4).

        Marks the LIVE notice superseded so a future, legitimately re-raised
        notice can claim the slot; history stays. Audited.
        """
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(
                update(notices_t)
                .where(
                    notices_t.c.opportunity_key == opportunity_key,
                    notices_t.c.superseded_at.is_(None),
                )
                .values(superseded_at=now, updated_at=now)
            )
        superseded = result.rowcount > 0
        if superseded:
            self._audit(owner_id, "initiative.superseded", opportunity_key)
        return superseded

    def _audit(
        self, owner_id: str, action: str, target: str, *, extra: dict[str, str] | None = None
    ) -> None:
        audit_service.record(
            engine=self._engine, user_id=owner_id, action=action, target=target, metadata=extra
        )
