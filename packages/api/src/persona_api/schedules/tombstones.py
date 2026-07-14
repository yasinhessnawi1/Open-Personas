"""The schedule-tombstone store — the durable user-intent record (Spec R9, R9-037).

The owner observed schedules they EDIT or DELETE getting silently re-created by the
persona's own autonomous machinery. The confirmed root cause (see
``docs/specs/phase3/spec_R9/evidence/R9-037-fix.md`` for the full evidence chain):
``persona_api.initiative.handler.ensure_initiative_schedule`` — the A5 per-persona
scan-schedule ensure — is a bare ``NOT EXISTS`` check with zero history awareness. It
is called both by the hourly (and immediate-on-every-worker-restart)
``InitiativeProvisioner`` sweep AND by the initiative dial chat-verb
(``InitiativeVerbService._apply_dial``); either call silently resurrects a schedule
the user just deleted.

:class:`ScheduleTombstoneStore` is the fix's substrate: every user delete/edit of a
schedule writes one row (:meth:`record`); every autonomous origination seam consults
:meth:`find_recent` before creating. Mirrors the ``DeclineStore`` shape (Spec A5,
``persona_api.initiative.store``) — the established house pattern for "a durable,
owner-scoped record of user intent that a later autonomous pass must consult."

Discipline (the ScheduleStore/DeclineStore template): CQS — reads never write,
mutators return the post-mutation record as confirmation; every mutation emits
exactly one ``audit_log`` row; every operation runs inside the owner's RLS GUC via
:func:`~persona_api.db.engine.rls_connection`, so a cross-tenant reach hits zero rows.

Matching v1 (the design's explicit scope): exact ``schedule_id`` identity OR
normalized ``(target_job_type, title_key)`` content identity — cheap, deterministic,
"exact-ish". Semantic-similarity matching is explicitly OUT of v1 (a follow-up).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select

from persona_api.db.engine import rls_connection
from persona_api.db.models import schedule_tombstones as tombstones_t
from persona_api.services import audit_service

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue
    from sqlalchemy import Engine, RowMapping

__all__ = [
    "ScheduleTombstoneStore",
    "TombstoneAction",
    "TombstoneRecord",
    "extract_subject",
    "normalize_title",
]


class TombstoneAction(StrEnum):
    """What the user did to the schedule (the ``action`` column's app-layer gate)."""

    DELETED = "deleted"
    EDITED = "edited"


class TombstoneRecord(BaseModel):
    """A tombstone row (frozen read/confirmation shape)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    owner_id: str
    schedule_id: str
    persona_id: str | None
    target_job_type: str
    title_key: str | None
    action: TombstoneAction
    reason: dict[str, str]
    created_at: datetime


def normalize_title(raw: str) -> str:
    """Collapse a subject string into the cheap, deterministic content-identity key.

    Lowercased + whitespace-collapsed — "exact-ish" matching (Matching v1): two
    subjects that read the same to a human but differ in case/spacing still match;
    anything beyond that (paraphrase, semantic similarity) is explicitly OUT of v1.
    """
    return " ".join(raw.strip().lower().split())


def extract_subject(payload_template: Mapping[str, JsonValue]) -> str | None:
    """A non-empty ``subject`` string from a schedule's payload template, or ``None``.

    The one narrowing point both tombstone-writing call sites (delete + edit) share —
    ``payload_template`` is untyped JSON, so a bare ``.get("subject")`` is a
    ``JsonValue | None`` union; this is the single place that turns it into a clean
    ``str | None`` (mypy-narrowed once, not re-derived at each call site).
    """
    raw = payload_template.get("subject")
    return raw if isinstance(raw, str) and raw.strip() else None


def _record_from_row(row: RowMapping) -> TombstoneRecord:
    return TombstoneRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        schedule_id=row["schedule_id"],
        persona_id=row["persona_id"],
        target_job_type=row["target_job_type"],
        title_key=row["title_key"],
        action=TombstoneAction(row["action"]),
        reason=dict(row["reason"] or {}),
        created_at=row["created_at"],
    )


class ScheduleTombstoneStore:
    """Owner-scoped, audited schedule-delete/edit intent records (R9-037).

    Construct with the ``persona_app`` RLS engine.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- reads (CQS: no writes) --------------------------------------------

    def find_recent(
        self,
        owner_id: str,
        *,
        window_days: int,
        now: datetime,
        schedule_id: str | None = None,
        target_job_type: str | None = None,
        title_key: str | None = None,
        action: TombstoneAction | None = None,
    ) -> TombstoneRecord | None:
        """The most recent matching tombstone within the window, or ``None``.

        Two independent match legs, tried in order (the caller supplies whichever
        applies to its seam):

        1. exact ``schedule_id`` identity — for a deterministic id re-derived on
           every attempt (e.g. ``initsched:{persona_id}``);
        2. normalized ``(target_job_type, title_key)`` content identity — for a
           seam whose id is freshly minted per attempt, so an id-only check would
           never catch a re-creation (the design's "idempotency keys alone are
           insufficient" — the check is against the tombstone record instead).

        At least one of ``schedule_id`` or (``target_job_type`` AND ``title_key``)
        must be given. ``now`` is the caller's clock frame (injected — core stays
        clock-free, mirrors ``InitiativeLedger.delivered_counts``). A non-positive
        ``window_days`` short-circuits to "never matches" (the env-tunable escape
        hatch — ``PERSONA_SCHEDULE_TOMBSTONE_WINDOW_DAYS=0`` disables gating).
        """
        if window_days <= 0:
            return None
        by_id = schedule_id is not None
        by_title = target_job_type is not None and title_key is not None
        if not by_id and not by_title:
            msg = "find_recent needs schedule_id or (target_job_type AND title_key)"
            raise ValueError(msg)
        cutoff = now - timedelta(days=window_days)
        conditions = []
        if by_id:
            conditions.append(tombstones_t.c.schedule_id == schedule_id)
        if by_title:
            conditions.append(
                (tombstones_t.c.target_job_type == target_job_type)
                & (tombstones_t.c.title_key == title_key)
            )
        predicate = conditions[0]
        for extra in conditions[1:]:
            predicate = predicate | extra
        clauses = [predicate, tombstones_t.c.created_at > cutoff]
        if action is not None:
            clauses.append(tombstones_t.c.action == action.value)
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(
                    select(tombstones_t)
                    .where(*clauses)
                    .order_by(tombstones_t.c.created_at.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return None if row is None else _record_from_row(row)

    # --- mutations (CQS: return the post-mutation record as confirmation) ---

    def record(
        self,
        owner_id: str,
        *,
        schedule_id: str,
        persona_id: str | None,
        target_job_type: str,
        title_key: str | None,
        action: TombstoneAction,
        reason: dict[str, str] | None,
        now: datetime,
    ) -> TombstoneRecord:
        """Write one tombstone row for a user delete/edit. Audits ``schedule.tombstone_recorded``.

        Append-only (never updated/deduped): a schedule deleted, re-created (by a
        legitimate NEW user action), and deleted again writes two rows — the full
        history is the audit-honest shape, and :meth:`find_recent` only ever reads
        the newest match within the window.
        """
        tombstone = TombstoneRecord(
            id=f"tomb_{uuid4().hex}",
            owner_id=owner_id,
            schedule_id=schedule_id,
            persona_id=persona_id,
            target_job_type=target_job_type,
            title_key=title_key,
            action=action,
            reason=reason or {},
            created_at=now,
        )
        with rls_connection(self._engine, owner_id) as conn:
            conn.execute(
                insert(tombstones_t).values(
                    id=tombstone.id,
                    owner_id=owner_id,
                    schedule_id=schedule_id,
                    persona_id=persona_id,
                    target_job_type=target_job_type,
                    title_key=title_key,
                    action=action.value,
                    reason=tombstone.reason,
                    created_at=now,
                )
            )
        self._audit(
            owner_id,
            "schedule.tombstone_recorded",
            schedule_id,
            extra={"action": action.value, "target_job_type": target_job_type},
        )
        return tombstone

    def audit_refusal(
        self,
        owner_id: str,
        *,
        schedule_id: str,
        seam: str,
        tombstone: TombstoneRecord,
    ) -> None:
        """Record that an origination seam TRIED to (re-)create a schedule and was
        stopped by a tombstone match (surface honesty — R9-037's design point 5).

        Not a write to ``schedule_tombstones`` itself — a pure ``audit_log`` note
        (``schedule.recreate_refused``) so "why didn't it do X" has a durable,
        greppable answer.
        """
        self._audit(
            owner_id,
            "schedule.recreate_refused",
            schedule_id,
            extra={
                "seam": seam,
                "tombstone_id": tombstone.id,
                "tombstone_action": tombstone.action.value,
                "tombstoned_at": tombstone.created_at.isoformat(),
            },
        )

    def _audit(
        self, owner_id: str, action: str, target: str, *, extra: dict[str, str] | None = None
    ) -> None:
        audit_service.record(
            engine=self._engine, user_id=owner_id, action=action, target=target, metadata=extra
        )
