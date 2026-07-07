"""The durable A7 event-trigger registry store — RLS-scoped CRUD + the atomic cooldown claim.

:class:`EventTriggerStore` owns the durable side of A7's registry: it persists (idempotently),
reads the match set, pauses/resumes/deletes, disables a platform's triggers on unlink, and — the
storm-safety primitive T3 builds on — **claims a fire atomically** (A7-D-4). Every operation runs
inside the owner's ``app.current_user_id`` GUC via :func:`~persona_api.db.engine.rls_connection`, so
a cross-tenant reach hits zero rows (the standing adversarial guarantee, proven non-vacuous under
the non-superuser role).

Discipline held here:

* **ONE write path for filter+platform** — :meth:`create_if_absent` derives ``platform`` from the
  ``filter`` via :func:`persona.events.platform_of` and writes both together; nothing else sets
  ``platform``. The column can never drift from the filter (the sync-tested invariant, T2 gate).
* **Idempotency rides the PK** — :meth:`create_if_absent` is ``INSERT … ON CONFLICT (id) DO
  NOTHING`` then a read, so a re-confirmed create reflects the existing row and NEVER errors.
* **The cooldown claim is one conditional UPDATE** — :meth:`claim_fire` fires iff its
  ``WHERE (last_fired_at IS NULL OR last_fired_at < now-cooldown)`` matches a row (RETURNING the
  coalesced burst count it resets); otherwise it atomically increments the pending count. Under
  concurrent arrivals READ-COMMITTED re-checks the predicate against the just-committed row, so
  exactly one arrival fires (the T3 storm test drives the concurrent case).
* **One ``audit_log`` row per mutation** — create/pause/resume/unlink-disable/delete each audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from persona.events import EventKind, TriggerAction, TriggerFilter, platform_of
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict, TypeAdapter
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.engine import aware_utc as _aware_utc
from persona_api.db.engine import rls_connection
from persona_api.db.models import event_triggers as triggers_t
from persona_api.services import audit_service

if TYPE_CHECKING:
    from sqlalchemy import Engine, RowMapping

__all__ = ["EventTriggerRecord", "EventTriggerStore", "FireClaim"]

_log = get_logger("api.events.store")

_FILTER_ADAPTER: TypeAdapter[TriggerFilter] = TypeAdapter(TriggerFilter)
_ACTION_ADAPTER: TypeAdapter[TriggerAction] = TypeAdapter(TriggerAction)


class EventTriggerRecord(BaseModel):
    """A stored event trigger — the registry row as a frozen, typed record.

    ``task_id`` is set for door-a (fire a confirmed task's leg) and ``None`` for door-b (enqueue an
    initiative candidate). ``platform`` is derived from ``filter`` by the store, never set directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    owner_id: str
    persona_id: str
    task_id: str | None
    event_kind: EventKind
    platform: str | None
    filter: TriggerFilter
    action: TriggerAction
    enabled: bool
    disabled_reason: str | None
    last_fired_at: datetime | None
    pending_coalesced_count: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class FireClaim:
    """The outcome of an atomic cooldown claim (A7-D-4).

    Attributes:
        fired: ``True`` iff this arrival won the cooldown window (it should enqueue the action).
        coalesced_count: On a fired claim, the number of events coalesced during the PREVIOUS window
            (0 on the first fire) — the "and N more" the fire reports. On a non-fired claim, the
            running pending count after this arrival incremented it.
    """

    fired: bool
    coalesced_count: int


def _record_from_row(row: RowMapping) -> EventTriggerRecord:
    """Map a registry row to the typed record (JSON columns re-validated to their unions)."""
    created = _aware_utc(row["created_at"])
    updated = _aware_utc(row["updated_at"])
    assert created is not None  # noqa: S101 — NOT NULL column
    assert updated is not None  # noqa: S101 — NOT NULL column
    return EventTriggerRecord(
        id=row["id"],
        owner_id=row["owner_id"],
        persona_id=row["persona_id"],
        task_id=row["task_id"],
        event_kind=EventKind(row["event_kind"]),
        platform=row["platform"],
        filter=_FILTER_ADAPTER.validate_python(row["filter"]),
        action=_ACTION_ADAPTER.validate_python(row["action"]),
        enabled=row["enabled"],
        disabled_reason=row["disabled_reason"],
        last_fired_at=_aware_utc(row["last_fired_at"]),
        pending_coalesced_count=row["pending_coalesced_count"],
        created_at=created,
        updated_at=updated,
    )


class EventTriggerStore:
    """RLS-scoped CRUD + lifecycle + the atomic cooldown claim for the A7 registry."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- reads (CQS) --------------------------------------------------------

    def get(self, owner_id: str, trigger_id: str) -> EventTriggerRecord | None:
        """Return one trigger, or ``None`` if absent (or another owner's — RLS hides it)."""
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(select(triggers_t).where(triggers_t.c.id == trigger_id))
                .mappings()
                .one_or_none()
            )
        return _record_from_row(row) if row is not None else None

    def list_active(self, owner_id: str, event_kind: EventKind) -> list[EventTriggerRecord]:
        """The dispatcher's match set: an owner's ENABLED triggers for one kind (indexed lookup)."""
        with rls_connection(self._engine, owner_id) as conn:
            rows = (
                conn.execute(
                    select(triggers_t).where(
                        triggers_t.c.event_kind == event_kind.value,
                        triggers_t.c.enabled.is_(True),
                    )
                )
                .mappings()
                .all()
            )
        return [_record_from_row(r) for r in rows]

    # --- mutations (CQS: return the post-mutation record as confirmation) ---

    def create_if_absent(self, record: EventTriggerRecord, *, now: datetime) -> EventTriggerRecord:
        """Persist a trigger idempotently; reflect the existing row on a PK conflict (never error).

        ``platform`` is (re)derived from ``record.filter`` here — the ONE write path — so it can
        never drift from the filter. A re-confirmed create (same deterministic ``id``) conflicts on
        the PK and returns the stored row unchanged (A4-D-X-create-seam idempotency).
        """
        platform = platform_of(record.filter)
        values = {
            "id": record.id,
            "owner_id": record.owner_id,
            "persona_id": record.persona_id,
            "task_id": record.task_id,
            "event_kind": record.event_kind.value,
            "platform": platform,
            "filter": record.filter.model_dump(mode="json"),
            "action": record.action.model_dump(mode="json"),
            "enabled": record.enabled,
            "disabled_reason": record.disabled_reason,
            "last_fired_at": record.last_fired_at,
            "pending_coalesced_count": record.pending_coalesced_count,
            "created_at": now,
            "updated_at": now,
        }
        with rls_connection(self._engine, record.owner_id) as conn:
            conn.execute(
                pg_insert(triggers_t).values(**values).on_conflict_do_nothing(index_elements=["id"])
            )
            row = (
                conn.execute(select(triggers_t).where(triggers_t.c.id == record.id))
                .mappings()
                .one()
            )
        self._audit(
            record.owner_id,
            "event_trigger.create",
            record.id,
            {"event_kind": record.event_kind.value, "action": record.action.kind},
        )
        return _record_from_row(row)

    def pause(self, owner_id: str, trigger_id: str, *, now: datetime) -> EventTriggerRecord | None:
        """Disable a trigger by user action ('user_paused'); returns the post-state or ``None``."""
        return self._set_enabled(
            owner_id,
            trigger_id,
            enabled=False,
            reason="user_paused",
            now=now,
            action="event_trigger.pause",
        )

    def resume(self, owner_id: str, trigger_id: str, *, now: datetime) -> EventTriggerRecord | None:
        """Re-enable a paused trigger (clears the reason); returns the post-state or ``None``."""
        return self._set_enabled(
            owner_id,
            trigger_id,
            enabled=True,
            reason=None,
            now=now,
            action="event_trigger.resume",
        )

    def disable_for_platform(self, owner_id: str, platform: str, *, now: datetime) -> int:
        """Disable ('unlinked') all of an owner's ENABLED triggers on a platform (criterion 7).

        The unlink-hygiene sweep: an unlinked platform's triggers go dormant with an audited reason
        and do NOT silently re-enable on re-link (re-enable is an explicit :meth:`resume`). Returns
        the number severed (0 ⇒ nothing to do).
        """
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(
                update(triggers_t)
                .where(
                    triggers_t.c.platform == platform,
                    triggers_t.c.enabled.is_(True),
                )
                .values(enabled=False, disabled_reason="unlinked", updated_at=now)
            )
            severed = result.rowcount
        if severed:
            self._audit(
                owner_id,
                "event_trigger.unlink_disabled",
                platform,
                {"platform": platform, "severed": str(severed)},
            )
        return severed

    def delete(self, owner_id: str, trigger_id: str) -> bool:
        """Delete a trigger; ``True`` if a row was removed. Audits ``event_trigger.delete``."""
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(delete(triggers_t).where(triggers_t.c.id == trigger_id))
            removed = result.rowcount > 0
        if removed:
            self._audit(owner_id, "event_trigger.delete", trigger_id, None)
        return removed

    def claim_fire(
        self, owner_id: str, trigger_id: str, *, now: datetime, cooldown_seconds: int
    ) -> FireClaim:
        """Atomically claim a fire (won the cooldown window) OR coalesce this arrival (A7-D-4).

        One conditional UPDATE: fire iff ``last_fired_at IS NULL OR last_fired_at < now-cooldown``,
        stamping ``last_fired_at=now`` and resetting the coalesced count (RETURNING the count that
        HAD accumulated — the "and N more"). If the window is still hot, no row returns; a second
        UPDATE increments the pending count. Under concurrent arrivals READ-COMMITTED re-evaluates
        the predicate against the just-committed row, so exactly one arrival fires.
        """
        threshold = now - timedelta(seconds=cooldown_seconds)
        claim_sql = text(
            "WITH prev AS (SELECT pending_coalesced_count AS c FROM event_triggers WHERE id = :id) "
            "UPDATE event_triggers AS t "
            "SET last_fired_at = :now, pending_coalesced_count = 0, updated_at = :now "
            "FROM prev "
            "WHERE t.id = :id "
            "  AND (t.last_fired_at IS NULL OR t.last_fired_at < :threshold) "
            "RETURNING prev.c AS coalesced"
        )
        coalesce_sql = text(
            "UPDATE event_triggers "
            "SET pending_coalesced_count = pending_coalesced_count + 1, updated_at = :now "
            "WHERE id = :id "
            "RETURNING pending_coalesced_count AS coalesced"
        )
        with rls_connection(self._engine, owner_id) as conn:
            claimed = (
                conn.execute(claim_sql, {"id": trigger_id, "now": now, "threshold": threshold})
                .mappings()
                .one_or_none()
            )
            if claimed is not None:
                return FireClaim(fired=True, coalesced_count=claimed["coalesced"])
            coalesced = (
                conn.execute(coalesce_sql, {"id": trigger_id, "now": now}).mappings().one_or_none()
            )
        # A missing row (deleted mid-flight) coalesces to nothing.
        pending = coalesced["coalesced"] if coalesced is not None else 0
        return FireClaim(fired=False, coalesced_count=pending)

    # --- internals ----------------------------------------------------------

    def _set_enabled(
        self,
        owner_id: str,
        trigger_id: str,
        *,
        enabled: bool,
        reason: str | None,
        now: datetime,
        action: str,
    ) -> EventTriggerRecord | None:
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(
                    update(triggers_t)
                    .where(triggers_t.c.id == trigger_id)
                    .values(enabled=enabled, disabled_reason=reason, updated_at=now)
                    .returning(triggers_t)
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        self._audit(owner_id, action, trigger_id, None)
        return _record_from_row(row)

    def _audit(
        self, owner_id: str, action: str, target: str, metadata: dict[str, str] | None
    ) -> None:
        audit_service.record(
            engine=self._engine, user_id=owner_id, action=action, target=target, metadata=metadata
        )
