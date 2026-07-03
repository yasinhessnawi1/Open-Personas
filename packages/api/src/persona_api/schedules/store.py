"""The durable schedule store — RLS-scoped CRUD + lifecycle (Spec A1, T4).

:class:`ScheduleStore` owns the durable side of A1's clock: it persists, reads,
edits, pauses/resumes, deletes, and records-fires-against schedules, all
**owner-scoped through RLS** (every operation runs inside the owner's
``app.current_user_id`` GUC via :func:`~persona_api.db.engine.rls_connection`),
so a cross-tenant reach hits zero rows — the standing adversarial guarantee.

Discipline held here:

* **CQS** — :meth:`get` / :meth:`list_for_owner` only read; the mutators only
  write and return the post-mutation :class:`~persona.schedules.Schedule` as the
  *confirmation* of the new state (the id/next-fire the caller needs), never a
  query result.
* **One ``AuditEvent`` per mutation** — exactly one ``audit_log`` row per
  create/edit/pause/resume/delete/fire (the project's auditability posture).
* **The ratified edit rule** — an edit recomputes ``next_fire_after(now)`` from
  the NEW rule but PRESERVES ``fire_count`` and ``created_at`` (the stable
  recurrence anchor), so there is no COUNT-reset loophole.
* **The next-fire invariant is centralised** — create/resume/edit/fire all set
  ``next_fire_at`` via the pure core :func:`~persona.schedules.next_fire_after`;
  no caller hand-computes it.

The cross-tenant scheduler tick (claiming due rows across owners, materialising
into A0 jobs) is a SEPARATE concern on the dispatch engine — T5/T6.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from persona.errors import (
    ScheduleConcurrentEditError,
    ScheduleNotFoundError,
    ScheduleStateError,
)
from persona.logging import get_logger
from persona.schedules import RecurrenceRule, Schedule, next_fire_after
from sqlalchemy import delete, insert, select, update

from persona_api.db.engine import rls_connection
from persona_api.db.models import schedules as schedules_t
from persona_api.services import audit_service

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Engine, RowMapping

__all__ = ["ScheduleStore"]

_log = get_logger("api.schedules.store")

# Bounded optimistic-CAS retries (A8-D-7). Contention is one schedule's edit coinciding
# with its own tick re-arm, so the loop essentially never spins; the bound is a backstop
# against genuine hot contention (→ ScheduleConcurrentEditError, never a lost edit).
_MAX_CAS_RETRIES = 5


def _recurrence_str(schedule: Schedule) -> str | None:
    """The RFC-5545 RRULE string for the durable column (None for a one-time)."""
    return schedule.recurrence.to_rrule_string() if schedule.recurrence is not None else None


def _row_to_schedule(row: RowMapping) -> Schedule:
    """Build a :class:`Schedule` from a ``schedules`` row (RRULE string → rule)."""
    recurrence_raw = row["recurrence"]
    recurrence = (
        RecurrenceRule.from_rrule_string(recurrence_raw) if recurrence_raw is not None else None
    )
    return Schedule(
        id=row["id"],
        owner_id=row["owner_id"],
        timezone=row["timezone"],
        recurrence=recurrence,
        one_time_at=row["one_time_at"],
        target_job_type=row["target_job_type"],
        payload_template=row["payload_template"],
        enabled=row["enabled"],
        paused=row["paused"],
        missed_fire_policy=row["missed_fire_policy"],
        grace_seconds=row["grace_seconds"],
        last_fire_at=row["last_fire_at"],
        next_fire_at=row["next_fire_at"],
        fire_count=row["fire_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        revision=row["revision"],
    )


def _values(schedule: Schedule) -> dict[str, Any]:
    """The full column value map for an INSERT/UPDATE from a Schedule."""
    return {
        "id": schedule.id,
        "owner_id": schedule.owner_id,
        "timezone": schedule.timezone,
        "recurrence": _recurrence_str(schedule),
        "one_time_at": schedule.one_time_at,
        "target_job_type": schedule.target_job_type,
        "payload_template": dict(schedule.payload_template),
        "enabled": schedule.enabled,
        "paused": schedule.paused,
        "missed_fire_policy": schedule.missed_fire_policy.value,
        "grace_seconds": schedule.grace_seconds,
        "last_fire_at": schedule.last_fire_at,
        "next_fire_at": schedule.next_fire_at,
        "fire_count": schedule.fire_count,
        "created_at": schedule.created_at,
        "updated_at": schedule.updated_at,
        "revision": schedule.revision,
    }


def _mutable_values(schedule: Schedule) -> dict[str, Any]:
    """The column subset an edit writes — everything but the immutable/CAS-managed keys.

    Excludes ``id`` / ``owner_id`` (the WHERE + scope) and ``revision`` (bumped by the
    CAS itself). ``created_at`` / ``fire_count`` / ``last_fire_at`` are carried on the
    merged Schedule already (the edit preserves the anchor) so writing them is a no-op.
    """
    values = _values(schedule)
    for key in ("id", "owner_id", "revision"):
        values.pop(key, None)
    return values


def _fire_values(schedule: Schedule) -> dict[str, Any]:
    """The column subset a fire/skip advance writes (bookkeeping only)."""
    return {
        "fire_count": schedule.fire_count,
        "last_fire_at": schedule.last_fire_at,
        "next_fire_at": schedule.next_fire_at,
        "updated_at": schedule.updated_at,
    }


class ScheduleStore:
    """Owner-scoped, audited CRUD + lifecycle over the ``schedules`` table.

    Construct with the ``persona_app`` RLS engine — every operation re-binds the
    owner's GUC, so the store can never reach another tenant's rows. ``audit_log``
    (non-RLS, INSERT-only for ``persona_app``) carries the per-mutation event.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- reads (CQS: no writes) --------------------------------------------

    def get(self, owner_id: str, schedule_id: str) -> Schedule:
        """Fetch one schedule. Raises :class:`ScheduleNotFoundError` on a miss.

        RLS-scoped: a cross-tenant id is indistinguishable from a missing one
        (both raise ``ScheduleNotFoundError`` — no existence oracle).
        """
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(select(schedules_t).where(schedules_t.c.id == schedule_id))
                .mappings()
                .first()
            )
        if row is None:
            raise ScheduleNotFoundError("schedule not found", context={"schedule_id": schedule_id})
        return _row_to_schedule(row)

    def list_for_owner(self, owner_id: str) -> list[Schedule]:
        """All of the owner's schedules, newest first (RLS-scoped read)."""
        with rls_connection(self._engine, owner_id) as conn:
            rows = (
                conn.execute(select(schedules_t).order_by(schedules_t.c.created_at.desc()))
                .mappings()
                .all()
            )
        return [_row_to_schedule(r) for r in rows]

    # --- mutations (CQS: return the post-mutation state as confirmation) ----

    def create(self, schedule: Schedule, *, now: datetime) -> Schedule:
        """Persist a new schedule with its initial ``next_fire_at`` computed.

        The first fire is the next occurrence strictly after the creation anchor
        (``schedule.created_at``); the store sets it so the tick picks the row up.
        Audits ``schedule.create``.
        """
        first_fire = next_fire_after(schedule, after=schedule.created_at)
        stored = schedule.with_next_fire(first_fire, now=now)
        with rls_connection(self._engine, schedule.owner_id) as conn:
            conn.execute(insert(schedules_t).values(**_values(stored)))
        self._audit(schedule.owner_id, "schedule.create", stored)
        return stored

    def edit(self, proposed: Schedule, *, now: datetime) -> Schedule:
        """Apply an edit; recompute ``next_fire_at(now)``; PRESERVE the anchor.

        The ratified rule (no COUNT-reset loophole): ``created_at`` and
        ``fire_count`` (and ``last_fire_at``) are taken from the CURRENT row, not
        from ``proposed`` — so a rule change cannot restart the recurrence anchor
        or the fire budget. Only the editable fields move; ``next_fire_at`` is
        recomputed from the new rule as of ``now``. Audits ``schedule.edit``.

        Optimistic-CAS guarded (A8-D-7): the write is ``WHERE revision = :expected``;
        a concurrent write (a tick re-arm, another edit) is retried a bounded number
        of times against the fresh row, so the edit lands correctly rather than being
        clobbered by — or clobbering — the racer. Persistent contention raises
        :class:`ScheduleConcurrentEditError`.
        """

        def _compute(current: Schedule) -> tuple[Schedule, dict[str, Any]]:
            merged = proposed.model_copy(
                update={
                    "created_at": current.created_at,  # stable recurrence anchor
                    "fire_count": current.fire_count,  # no COUNT reset
                    "last_fire_at": current.last_fire_at,
                }
            )
            next_fire = next_fire_after(merged, after=now)
            merged = merged.with_next_fire(next_fire, now=now)
            return merged, _mutable_values(merged)

        return self._mutate(
            proposed.owner_id, proposed.id, compute=_compute, action="schedule.edit"
        )

    def pause(self, owner_id: str, schedule_id: str, *, now: datetime) -> Schedule:
        """Pause a schedule (stops firing, preserves the rule). Audits ``schedule.pause``.

        ``next_fire_at`` is left as-is; :meth:`resume` recomputes it so a long
        pause never fires a stale past time. CAS-guarded (A8-D-7 uniform invariant).
        """

        def _compute(current: Schedule) -> tuple[Schedule, dict[str, Any]]:
            paused = current.model_copy(update={"paused": True, "updated_at": now})
            return paused, {"paused": True, "updated_at": now}

        return self._mutate(owner_id, schedule_id, compute=_compute, action="schedule.pause")

    def resume(self, owner_id: str, schedule_id: str, *, now: datetime) -> Schedule:
        """Resume a paused schedule; recompute ``next_fire_at(now)``. Audits ``schedule.resume``.

        Recomputing from ``now`` (criterion 7) means a schedule resumed after a
        long pause fires next on its rhythm, not a stale missed instant. CAS-guarded.
        """

        def _compute(current: Schedule) -> tuple[Schedule, dict[str, Any]]:
            next_fire = next_fire_after(current, after=now)
            resumed = current.model_copy(
                update={"paused": False, "next_fire_at": next_fire, "updated_at": now}
            )
            return resumed, {"paused": False, "next_fire_at": next_fire, "updated_at": now}

        return self._mutate(owner_id, schedule_id, compute=_compute, action="schedule.resume")

    def record_fire(self, owner_id: str, schedule_id: str, *, fire_time: datetime) -> Schedule:
        """Record a fire, auto-advancing ``next_fire_at`` to the next occurrence.

        The new ``next_fire_at`` is the next occurrence strictly after
        ``fire_time`` (``None`` when the rule is exhausted or a one-time has fired
        — one-time COMPLETION). Audits ``schedule.fire``. For the tick's coalesced
        advance (next occurrence after *now*, the no-burst path), use
        :meth:`apply_fire` with an explicit ``next_fire_at``. CAS-guarded.
        """

        def _compute(current: Schedule) -> tuple[Schedule, dict[str, Any]]:
            next_fire = next_fire_after(current, after=fire_time)
            fired = current.record_fire(fire_time=fire_time, next_fire_at=next_fire)
            return fired, _fire_values(fired)

        return self._mutate(
            owner_id,
            schedule_id,
            compute=_compute,
            action="schedule.fire",
            extra={"fire_time": fire_time.isoformat()},
        )

    def skip_next(self, owner_id: str, schedule_id: str, *, now: datetime) -> Schedule:
        """Suppress exactly the NEXT occurrence; the following one is unaffected (A8-D-2).

        Advances ``next_fire_at`` PAST the next due occurrence to the one after it, WITHOUT
        firing (no ``fire_count`` bump), and audits ``schedule.skip_next`` with the suppressed
        instant. The next real fire is then the FOLLOWING occurrence — proven on the real
        scheduler. CAS-guarded (retries on a concurrent write). Raises
        :class:`ScheduleStateError` if there is no next occurrence (a completed schedule).
        """
        for _ in range(_MAX_CAS_RETRIES):
            current = self.get(owner_id, schedule_id)
            suppressed = current.next_fire_at
            if suppressed is None:
                raise ScheduleStateError(
                    "no next occurrence to skip",
                    context={"schedule_id": schedule_id, "operation": "skip_next"},
                )
            following = next_fire_after(current, after=suppressed)
            updated = current.with_next_fire(following, now=now)
            values = {"next_fire_at": following, "updated_at": updated.updated_at}
            if self._cas_update(owner_id, schedule_id, values, expected=current.revision):
                bumped = updated.model_copy(update={"revision": current.revision + 1})
                self._audit(
                    owner_id,
                    "schedule.skip_next",
                    bumped,
                    extra={"suppressed_fire_time": suppressed.isoformat()},
                )
                return bumped
        raise ScheduleConcurrentEditError(
            "skip-next lost the concurrency race",
            context={"schedule_id": schedule_id, "attempts": str(_MAX_CAS_RETRIES)},
        )

    def apply_fire(
        self,
        owner_id: str,
        schedule_id: str,
        *,
        fire_time: datetime,
        next_fire_at: datetime | None,
        now: datetime,
        expected_revision: int,
        late: bool = False,
    ) -> Schedule:
        """Record a fire from the tick, CAS-guarded against a mid-flight edit (A8-D-7).

        The tick claims a due row (at ``expected_revision``), computes
        ``next_fire_at = next_fire_after(now)`` (the no-burst coalesce), enqueues the
        job (idempotent), then calls this to advance the bookkeeping. The write is a
        compare-and-swap on ``expected_revision``:

        * **CAS hit** — the row is untouched since the claim; record the fire against
          the tick's computed ``next_fire_at``.
        * **CAS miss** — a write landed between claim and re-arm. RECONCILE
          (:meth:`_reconcile_fire`): if THIS fire was already recorded by a concurrent
          tick (``last_fire_at == fire_time`` — the I3 guard) it is a no-op; otherwise
          an edit landed, so record the fire against the CURRENT (edited) rule, never
          the tick's stale ``next_fire_at``.

        ``late`` flags a fire-late-once catch-up (``schedule.fire_late``). A one-time
        schedule completes regardless (the entity forces ``None``).
        """
        current = self.get(owner_id, schedule_id)
        if current.last_fire_at == fire_time:  # already recorded (I3 idempotent bookkeeping)
            return current
        fired = current.record_fire(fire_time=fire_time, next_fire_at=next_fire_at)
        if self._cas_update(owner_id, schedule_id, _fire_values(fired), expected=expected_revision):
            bumped = fired.model_copy(update={"revision": expected_revision + 1})
            self._audit_fire(owner_id, bumped, fire_time=fire_time, late=late)
            return bumped
        return self._reconcile_fire(owner_id, schedule_id, fire_time=fire_time, now=now, late=late)

    def skip_fire(
        self,
        owner_id: str,
        schedule_id: str,
        *,
        missed_fire_time: datetime,
        next_fire_at: datetime | None,
        now: datetime,
        expected_revision: int,
    ) -> Schedule:
        """Skip a missed fire: advance ``next_fire_at``, record a durable miss note.

        Used when the missed-fire policy declines to fire (skip-and-note, or
        fire-late-once beyond grace). It does NOT enqueue a job and does NOT bump
        ``fire_count`` (no fire happened); it only advances next-fire to the next
        future occurrence (the no-burst floor) and emits a durable ``schedule.miss``
        audit note. CAS-guarded on the claimed ``expected_revision``: if an edit
        landed the reconcile re-advances from the CURRENT (edited) rule; if the row
        already advanced past the missed instant it is a no-op. A skipped one-time
        (``next_fire_at`` resolves to ``None``) terminates.
        """
        current = self.get(owner_id, schedule_id)
        updated = current.with_next_fire(next_fire_at, now=missed_fire_time)
        values = {"next_fire_at": next_fire_at, "updated_at": updated.updated_at}
        if self._cas_update(owner_id, schedule_id, values, expected=expected_revision):
            bumped = updated.model_copy(update={"revision": expected_revision + 1})
            self._audit(
                owner_id,
                "schedule.miss",
                bumped,
                extra={"missed_fire_time": missed_fire_time.isoformat()},
            )
            return bumped
        return self._reconcile_skip(
            owner_id, schedule_id, missed_fire_time=missed_fire_time, now=now
        )

    def _reconcile_fire(
        self, owner_id: str, schedule_id: str, *, fire_time: datetime, now: datetime, late: bool
    ) -> Schedule:
        """CAS-miss reconciliation for a fire (A8-D-7): record it once, against the edited rule."""
        for _ in range(_MAX_CAS_RETRIES):
            current = self.get(owner_id, schedule_id)
            if current.last_fire_at == fire_time:  # a concurrent tick already recorded it (I3)
                return current
            next_fire = next_fire_after(current, after=now)  # from the CURRENT (edited) rule
            fired = current.record_fire(fire_time=fire_time, next_fire_at=next_fire)
            if self._cas_update(
                owner_id, schedule_id, _fire_values(fired), expected=current.revision
            ):
                bumped = fired.model_copy(update={"revision": current.revision + 1})
                self._audit_fire(owner_id, bumped, fire_time=fire_time, late=late)
                return bumped
        raise ScheduleConcurrentEditError(
            "fire re-arm lost the concurrency race",
            context={"schedule_id": schedule_id, "attempts": str(_MAX_CAS_RETRIES)},
        )

    def _reconcile_skip(
        self, owner_id: str, schedule_id: str, *, missed_fire_time: datetime, now: datetime
    ) -> Schedule:
        """CAS-miss reconciliation for a skip: re-advance from the edited rule, once."""
        for _ in range(_MAX_CAS_RETRIES):
            current = self.get(owner_id, schedule_id)
            if current.next_fire_at is None or current.next_fire_at > missed_fire_time:
                return current  # already advanced past the miss (edit / concurrent handling)
            next_fire = next_fire_after(current, after=now)
            updated = current.with_next_fire(next_fire, now=missed_fire_time)
            values = {"next_fire_at": next_fire, "updated_at": updated.updated_at}
            if self._cas_update(owner_id, schedule_id, values, expected=current.revision):
                bumped = updated.model_copy(update={"revision": current.revision + 1})
                self._audit(
                    owner_id,
                    "schedule.miss",
                    bumped,
                    extra={"missed_fire_time": missed_fire_time.isoformat()},
                )
                return bumped
        raise ScheduleConcurrentEditError(
            "skip re-arm lost the concurrency race",
            context={"schedule_id": schedule_id, "attempts": str(_MAX_CAS_RETRIES)},
        )

    def _mutate(
        self,
        owner_id: str,
        schedule_id: str,
        *,
        compute: Callable[[Schedule], tuple[Schedule, dict[str, Any]]],
        action: str,
        extra: dict[str, str] | None = None,
    ) -> Schedule:
        """Read-modify-write with optimistic-CAS retry (the non-tick mutation path).

        ``compute(current)`` returns the post-mutation :class:`Schedule` + the column
        subset to write; the CAS bumps ``revision`` and guards ``WHERE revision =
        current.revision``. A miss (a concurrent write) re-reads and retries; a
        genuine miss on the row surfaces as ``ScheduleNotFoundError`` on the next
        ``get``. Bounded — persistent contention raises ``ScheduleConcurrentEditError``.
        """
        for _ in range(_MAX_CAS_RETRIES):
            current = self.get(owner_id, schedule_id)
            new_schedule, values = compute(current)
            if self._cas_update(owner_id, schedule_id, values, expected=current.revision):
                bumped = new_schedule.model_copy(update={"revision": current.revision + 1})
                self._audit(owner_id, action, bumped, extra=extra)
                return bumped
        raise ScheduleConcurrentEditError(
            "schedule mutation lost the concurrency race",
            context={"schedule_id": schedule_id, "attempts": str(_MAX_CAS_RETRIES)},
        )

    def _cas_update(
        self, owner_id: str, schedule_id: str, values: dict[str, Any], *, expected: int
    ) -> bool:
        """Compare-and-swap UPDATE: SET ``values`` + ``revision+1`` WHERE id AND revision=expected.

        Returns ``True`` iff exactly one row matched (the CAS held); ``False`` on a
        revision miss OR an absent row (the caller's next ``get`` distinguishes them).
        """
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(
                update(schedules_t)
                .where(schedules_t.c.id == schedule_id, schedules_t.c.revision == expected)
                .values(**values, revision=schedules_t.c.revision + 1)
            )
        return result.rowcount == 1

    def _audit_fire(
        self, owner_id: str, schedule: Schedule, *, fire_time: datetime, late: bool
    ) -> None:
        """Emit the per-fire audit note (``schedule.fire`` / ``schedule.fire_late``)."""
        action = "schedule.fire_late" if late else "schedule.fire"
        self._audit(owner_id, action, schedule, extra={"fire_time": fire_time.isoformat()})

    def delete(self, owner_id: str, schedule_id: str) -> None:
        """Delete a schedule. Raises :class:`ScheduleNotFoundError` if absent.

        RLS-scoped: a cross-tenant delete affects zero rows and raises
        ``ScheduleNotFoundError`` (no oracle). CQS: returns nothing. Audits
        ``schedule.delete`` only on a real deletion.
        """
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(delete(schedules_t).where(schedules_t.c.id == schedule_id))
            if result.rowcount != 1:
                raise ScheduleNotFoundError(
                    "schedule not found", context={"schedule_id": schedule_id}
                )
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action="schedule.delete",
            target=schedule_id,
            metadata={},
        )

    # --- internals ----------------------------------------------------------

    def _audit(
        self,
        owner_id: str,
        action: str,
        schedule: Schedule,
        *,
        extra: dict[str, str] | None = None,
    ) -> None:
        """Emit exactly one ``audit_log`` row for a schedule mutation.

        ``extra`` carries event-specific fields (e.g. the fired/missed scheduled
        instant) so the durable note is self-describing for A3 honesty / A6 display.
        """
        metadata: dict[str, str] = {
            "target_job_type": schedule.target_job_type,
            "fire_count": str(schedule.fire_count),
            "next_fire_at": schedule.next_fire_at.isoformat()
            if schedule.next_fire_at is not None
            else "",
        }
        if extra:
            metadata.update(extra)
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action=action,
            target=schedule.id,
            metadata=metadata,
        )
