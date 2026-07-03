"""The scheduler tick — leader-gated due-claim + cross-tenant materialisation (Spec A1, T6).

The tick is A1's clock hand. When (and only when) this worker holds leadership
(D-A1-5), one tick:

1. **Claims due schedules cross-tenant** — a global poll on the DISPATCH engine
   (``next_fire_at <= now``, served by the partial ``idx_schedules_due`` index),
   so it sees every owner's due rows. This is the dispatch side of the
   dispatch-vs-RLS split (the worker's dispatch engine sees all tenants).
2. **Materialises each due fire FOR THAT SCHEDULE'S OWNER** — for each due
   schedule it switches to the owner's scope and (a) enqueues an A0 job via
   ``JobQueue`` (owner-scoped RLS engine + owner GUC) keyed by the deterministic
   ``schedule_id+fire_time`` idempotency key, then (b) advances the schedule's
   bookkeeping (owner-scoped ``ScheduleStore.apply_fire``). Both are owner-scoped
   — the RLS side of the split.

**Effectively-once (criterion 4).** The idempotency key makes a double tick, a
leader handover mid-tick, or a crash-rerun all dedup to exactly one A0 job per
``(schedule, fire_time)`` — A1 rides A0's ``ON CONFLICT DO NOTHING``, it does not
reinvent effectively-once. Enqueue happens BEFORE the bookkeeping advance, so a
crash between the two re-enqueues (a no-op) on the next tick rather than skipping.

**No burst (the structural invariant).** The advance is
``next_fire_after(now)`` — a backlog of missed occurrences collapses to a single
fire that jumps to the next FUTURE occurrence, never a one-per-tick replay. The
fired scheduled instant (``fire_time``) is what the job carries (criterion 8), so
a late fire still says "this is the 7am briefing for that morning." The
fire-late-once-vs-skip-and-note POLICY + grace window + durable miss note (the
nuance on top of this structural floor) land in T7.

**The handoff contract (criterion 8).** Every fired job's payload carries
``schedule_id`` + ``fire_time`` (``persona.schedules.fire_payload``), so a
downstream A2 leg can anchor on which schedule + which scheduled instant it is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.schedules import (
    FireAction,
    decide_fire,
    fire_idempotency_key,
    fire_payload,
    next_fire_after,
)
from sqlalchemy import select

from persona_api.db.models import schedules as schedules_t
from persona_api.jobs.queue import JobQueue
from persona_api.schedules.leadership import SchedulerLeader
from persona_api.schedules.store import ScheduleStore, _row_to_schedule

if TYPE_CHECKING:
    from persona.schedules import Schedule
    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = ["SchedulerTick", "build_scheduler_tick"]

_log = get_logger("api.schedules.tick")


class SchedulerTick:
    """Leader-gated scheduler tick: claim due schedules, materialise A0 jobs.

    Args:
        dispatch_engine: The cross-tenant engine for the global due-claim (sees
            all owners' schedules — the worker's dispatch engine).
        rls_engine: The ``persona_app`` owner-scoped engine for the per-schedule
            enqueue + bookkeeping (the materialisation runs in the owner's scope).
        leader: The leadership lock — the tick is a no-op unless this worker leads.
        batch_size: Max due schedules materialised per tick.
        default_grace_seconds: Kind-relative catch-up window for a RECURRING
            schedule with no per-schedule override (D-A1-2 — daily ≈ 2–3h).
        one_time_grace_seconds: Catch-up window for a ONE-TIME schedule with no
            override.
        on_time_tolerance_seconds: How late still counts as "caught promptly" (the
            normal next-tick latency), firing regardless of policy.
    """

    def __init__(
        self,
        *,
        dispatch_engine: Engine,
        rls_engine: Engine,
        leader: SchedulerLeader,
        batch_size: int = 100,
        default_grace_seconds: float = 10_800.0,
        one_time_grace_seconds: float = 3_600.0,
        on_time_tolerance_seconds: float = 120.0,
    ) -> None:
        self._dispatch_engine = dispatch_engine
        self._leader = leader
        self._store = ScheduleStore(rls_engine)
        self._queue = JobQueue(rls_engine)
        self._batch_size = batch_size
        self._default_grace_seconds = default_grace_seconds
        self._one_time_grace_seconds = one_time_grace_seconds
        self._on_time_tolerance_seconds = on_time_tolerance_seconds

    def run_once(self, *, now: datetime | None = None) -> int:
        """Run one tick. Returns the number of schedules fired (0 if not leader).

        Gated by leadership: a follower returns 0 without touching the DB. The
        leader claims due schedules and materialises each. Each materialisation is
        independent — one schedule's failure is logged and skipped, never aborting
        the whole tick (a poison schedule must not stall the clock for everyone).
        """
        if not self._leader.try_become_leader():
            return 0
        now = now if now is not None else datetime.now(UTC)
        due = self._claim_due(now)
        fired = 0
        skipped = 0
        for schedule in due:
            try:
                action = self._materialise(schedule, now=now)
                if action is FireAction.SKIP:
                    skipped += 1
                else:
                    fired += 1
            except Exception:  # noqa: BLE001 — one bad schedule must not stall the tick
                _log.exception(
                    "schedule materialisation failed; skipping",
                    schedule_id=schedule.id,
                    owner_id=schedule.owner_id,
                )
        if fired or skipped:
            _log.info("scheduler tick processed due schedules", fired=fired, skipped=skipped)
        return fired

    def _claim_due(self, now: datetime) -> list[Schedule]:
        """Read all due schedules across owners (cross-tenant, dispatch engine).

        ``enabled AND NOT paused AND next_fire_at IS NOT NULL AND next_fire_at <=
        now``, oldest-due first — the predicate the partial ``idx_schedules_due``
        index serves. A plain read (no row lock): the idempotency key, not a lock,
        is the overlap-correctness floor (D-A1-5), so two briefly-overlapping ticks
        are harmless.
        """
        stmt = (
            select(schedules_t)
            .where(
                schedules_t.c.enabled,
                ~schedules_t.c.paused,
                schedules_t.c.next_fire_at.isnot(None),
                schedules_t.c.next_fire_at <= now,
            )
            .order_by(schedules_t.c.next_fire_at)
            .limit(self._batch_size)
        )
        with self._dispatch_engine.connect() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [_row_to_schedule(r) for r in rows]

    def _materialise(self, schedule: Schedule, *, now: datetime) -> FireAction:
        """Apply the missed-fire policy to one due fire; return the action taken.

        The decision (``decide_fire``) gates whether the fire materialises:

        * ``FIRE`` / ``FIRE_LATE`` — owner-scoped enqueue (deduped by the
          ``schedule_id+fire_time`` key, criterion 4; handoff anchor in the
          payload, criterion 8), THEN advance bookkeeping. Enqueue-first so a crash
          between the two re-enqueues (a no-op) rather than skipping.
        * ``SKIP`` — no enqueue; record a durable miss note + advance.

        In every case ``next_fire_at`` coalesces to the next occurrence after NOW
        (the structural no-burst floor), so a backlog never replays.
        """
        fire_time = schedule.next_fire_at
        if fire_time is None:  # pragma: no cover — the claim filters these out
            return FireAction.SKIP
        next_fire = next_fire_after(schedule, after=now)
        lateness = (now - fire_time).total_seconds()
        grace = (
            schedule.grace_seconds
            if schedule.grace_seconds is not None
            else (
                self._one_time_grace_seconds
                if schedule.is_one_time
                else self._default_grace_seconds
            )
        )
        action = decide_fire(
            policy=schedule.missed_fire_policy,
            lateness_seconds=lateness,
            grace_seconds=grace,
            on_time_tolerance_seconds=self._on_time_tolerance_seconds,
        )
        if action is FireAction.SKIP:
            # Worker was down past the catch-up window (or skip-and-note): record a
            # durable miss note, advance past the backlog, do NOT fire. CAS-guarded on
            # the claimed revision so a concurrent edit is reconciled, not clobbered.
            self._store.skip_fire(
                schedule.owner_id,
                schedule.id,
                missed_fire_time=fire_time,
                next_fire_at=next_fire,
                now=now,
                expected_revision=schedule.revision,
            )
            return action
        self._queue.enqueue(
            type=schedule.target_job_type,
            owner_id=schedule.owner_id,
            payload=fire_payload(schedule.id, fire_time, schedule.payload_template),
            idempotency_key=fire_idempotency_key(schedule.id, fire_time),
        )
        # Enqueue-first (idempotent) THEN advance the bookkeeping, CAS-guarded on the
        # revision claimed above: an edit landing between the claim and here is
        # reconciled (record the fire against the edited rule), and a concurrent tick's
        # duplicate advance is a no-op — neither double-fires nor drops (A8-D-7).
        self._store.apply_fire(
            schedule.owner_id,
            schedule.id,
            fire_time=fire_time,
            next_fire_at=next_fire,
            now=now,
            expected_revision=schedule.revision,
            late=action is FireAction.FIRE_LATE,
        )
        return action


def build_scheduler_tick(
    config: APIConfig, *, dispatch_engine: Engine, rls_engine: Engine
) -> SchedulerTick:
    """Compose a :class:`SchedulerTick` from config — the tick's composition seam.

    Builds the :class:`SchedulerLeader` on the cross-tenant ``dispatch_engine`` (the
    leader's held session connection routes there) and wires the config-driven
    batch size + grace/tolerance windows (D-A1-2/D-A1-3). The worker composition
    root calls this and passes the result to :class:`~persona_api.jobs.worker.Worker`
    (the additive ``scheduler_tick`` param). The api→worker scheduler deploy is the
    orchestrator's, exactly like A0's worker cutover.
    """
    leader = SchedulerLeader(dispatch_engine)
    return SchedulerTick(
        dispatch_engine=dispatch_engine,
        rls_engine=rls_engine,
        leader=leader,
        batch_size=config.scheduler_batch_size,
        default_grace_seconds=config.scheduler_default_grace_seconds,
        one_time_grace_seconds=config.scheduler_one_time_grace_seconds,
        on_time_tolerance_seconds=config.scheduler_on_time_tolerance_seconds,
    )
