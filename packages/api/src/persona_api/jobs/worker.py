"""The worker service — the fourth process class (Spec A0, T4).

A long-lived process that claims durable jobs and runs them through the same
runtime the api uses (one runtime, two execution paths). This module owns the
worker's **composition root** — the worker's analogue of ``app.py``'s lifespan —
wiring the two engines that enforce the RLS boundary:

- **dispatch engine** (cross-tenant): claim/heartbeat/complete on the jobs tables
  only. v0.1 defaults to the superuser ``database_url``; point
  ``WORKER_DISPATCH_DATABASE_URL`` at a least-privilege ``job_dispatcher`` role
  to harden (pure config — D-A0-X-rls-chokepoint).
- **RLS engine** (``persona_app``, owner-scoped): the only engine handlers touch,
  via the per-job :class:`WorkerJobContext`.

The two are kept structurally apart: the dispatch engine reaches the queue, never
a handler; the RLS engine reaches handlers, never the cross-tenant claim. The
continuous poll loop + graceful drain (signals, drain bound) land in T5; T4
provides :meth:`Worker.run_once` (claim a batch, execute each) and the health
probes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from persona.jobs import MEDIUM_LEASE, JobRegistry
from persona.logging import get_logger
from sqlalchemy import text

from persona_api.db.engine import create_db_engine
from persona_api.editions import check_cloud_config_guard
from persona_api.jobs.executor import JobExecutor
from persona_api.jobs.queue import JobQueue
from persona_api.middleware.rls_context import make_rls_engine

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy import Engine

    # A3 lifecycle sweeps plug in the same additive way (Spec A3, T9/T13): the approval
    # reminder/expiry sweep + the dead-leg voicing sweep, each leader-gated on its own
    # advisory key. None on a worker without them → behaves exactly as before.
    from persona_api.approvals.sweep import ApprovalSweepRunner
    from persona_api.config import APIConfig

    # N2's catalog auto-sync plugs into the loop the same additive way (Spec N2, T3):
    # a third ownerless periodic task; None on a worker without it → behaves as before.
    from persona_api.initiative.provisioner import InitiativeProvisioner
    from persona_api.jobs.catalog_sync import CatalogSyncTask

    # S2's skill-catalog auto-sync plugs in the same additive way (Spec S2, C1): a fourth
    # ownerless periodic task on the same substrate; None on a worker without it.
    from persona_api.jobs.skill_catalog_sync import SkillCatalogSyncTask

    # A1's scheduler tick plugs into the loop additively (Spec A1, T6). Imported
    # under TYPE_CHECKING only, so the A0 worker keeps ZERO runtime dependency on
    # A1 — a worker built without a tick behaves exactly as A0 shipped.
    from persona_api.schedules.tick import SchedulerTick
    from persona_api.tasks.dead_leg_sweep import DeadLegSweeper

# Signals that initiate a graceful drain: Fly sends SIGINT by default and
# SIGTERM when configured (we trap both — D-A0-5).
_DRAIN_SIGNALS = (signal.SIGTERM, signal.SIGINT)

__all__ = ["Worker", "build_worker", "make_worker_id"]

_log = get_logger("api.jobs.worker")


def make_worker_id() -> str:
    """A unique-per-process worker identity for lease ownership.

    ``host:pid:rand`` — the random suffix disambiguates a recycled PID on the same
    host (two workers must never share an id, or one could renew/complete the
    other's lease).
    """
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(3)}"


class Worker:
    """Claims and executes durable jobs. Holds the two RLS-separated engines.

    Args:
        dispatch_engine: Cross-tenant engine for jobs-table dispatch ops.
        rls_engine: The ``persona_app`` engine handlers' owner-scoped contexts
            are built on (never used for claim/dispatch).
        registry: The typed handler registry.
        worker_id: This process's identity (defaults to ``host:pid``).
    """

    def __init__(
        self,
        *,
        dispatch_engine: Engine,
        rls_engine: Engine,
        registry: JobRegistry,
        worker_id: str | None = None,
        concurrency: int = 4,
        poll_interval_seconds: float = 1.0,
        poll_jitter_seconds: float = 0.5,
        claim_lease_seconds: int = MEDIUM_LEASE.lease_seconds,
        drain_seconds: float = 270.0,
        max_jobs_per_user: int = 0,
        max_jobs_global: int = 0,
        maintenance_interval_seconds: float = 30.0,
        archive_after_seconds: float = 86_400.0,
        archive_retention_seconds: float = 2_592_000.0,
        scheduler_tick: SchedulerTick | None = None,
        scheduler_tick_interval_seconds: float = 30.0,
        catalog_sync: CatalogSyncTask | None = None,
        catalog_sync_interval_seconds: float = 86_400.0,
        skill_catalog_sync: SkillCatalogSyncTask | None = None,
        skill_catalog_sync_interval_seconds: float = 86_400.0,
        initiative_provisioner: InitiativeProvisioner | None = None,
        initiative_provisioner_interval_seconds: float = 3_600.0,
        approval_sweep: ApprovalSweepRunner | None = None,
        approval_sweep_interval_seconds: float = 300.0,
        dead_leg_sweep: DeadLegSweeper | None = None,
        dead_leg_sweep_interval_seconds: float = 120.0,
    ) -> None:
        self._dispatch_engine = dispatch_engine
        self._rls_engine = rls_engine
        self._worker_id = worker_id or make_worker_id()
        self._queue = JobQueue(dispatch_engine)
        self._executor = JobExecutor(
            queue=self._queue,
            registry=registry,
            rls_engine=rls_engine,
            worker_id=self._worker_id,
        )
        self._concurrency = concurrency
        self._poll_interval = poll_interval_seconds
        self._poll_jitter = poll_jitter_seconds
        self._claim_lease_seconds = claim_lease_seconds
        self._drain_seconds = drain_seconds
        self._max_jobs_per_user = max_jobs_per_user
        self._max_jobs_global = max_jobs_global
        self._maintenance_interval = maintenance_interval_seconds
        self._archive_after = archive_after_seconds
        self._archive_retention = archive_retention_seconds
        # A1 scheduler tick (additive; None on a plain A0 worker — D-A1-X-worker-additive).
        self._scheduler_tick = scheduler_tick
        self._scheduler_tick_interval = scheduler_tick_interval_seconds
        # N2 catalog auto-sync (additive; None when disabled/unwired — N2-D-1/3).
        self._catalog_sync = catalog_sync
        self._catalog_sync_interval = catalog_sync_interval_seconds
        # S2 skill-catalog auto-sync (additive; None when disabled/unwired — S2-D-1, a second
        # ownerless periodic on the same substrate, distinct leader key).
        self._skill_catalog_sync = skill_catalog_sync
        self._skill_catalog_sync_interval = skill_catalog_sync_interval_seconds
        # Spec A5 (T10): the leader-gated schedule-provisioning sweep (population-level
        # built-but-inert killer). None → inert (initiative disabled).
        self._initiative_provisioner = initiative_provisioner
        self._initiative_provisioner_interval = initiative_provisioner_interval_seconds
        # Spec A3 (T9/T13): the two lifecycle sweeps — approval reminder/expiry + dead-leg
        # voicing. Additive; None on a worker without them → the loop is unchanged. Each is
        # leader-gated on its OWN advisory key inside run_once and best-effort (a failure is
        # logged, never crashing the loop).
        self._approval_sweep = approval_sweep
        self._approval_sweep_interval = approval_sweep_interval_seconds
        self._dead_leg_sweep = dead_leg_sweep
        self._dead_leg_sweep_interval = dead_leg_sweep_interval_seconds
        self._draining = asyncio.Event()
        self._in_flight: set[asyncio.Task[object]] = set()
        self._last_maintenance = 0.0
        self._last_scheduler_tick = 0.0
        # None = "never synced" → run once shortly after boot (a fresh mirror on deploy),
        # then on the daily-ish cadence. A 0.0 seed would instead defer the first sync by a
        # full interval, since the monotonic clock starts small on a fresh container.
        self._last_catalog_sync: float | None = None
        self._last_skill_catalog_sync: float | None = None
        self._last_initiative_provision: float | None = None
        self._last_approval_sweep: float | None = None
        self._last_dead_leg_sweep: float | None = None
        # R9-093 observability: wall-clock of the last completed loop iteration.
        # ``None`` until the loop first turns. A dead loop leaves this frozen,
        # which is what makes "the background half stopped" OBSERVABLE — the
        # 2026-08-01 stall was invisible for an hour precisely because a dead
        # loop and an idle one looked identical from outside the process.
        # Wall-clock (not monotonic) so it can be reported and compared directly.
        self._last_beat_at: datetime | None = None

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def last_beat_at(self) -> datetime | None:
        """When the loop last completed an iteration, or ``None`` if never.

        The liveness signal for :func:`~persona_api.routes.health.healthz`.
        Supervision (R9-093) restarts a CRASHED loop; this catches the other
        shape — a loop that is alive but wedged (a hung await), which no
        restart-on-exception can detect because nothing ever raises.
        """
        return self._last_beat_at

    def request_drain(self) -> None:
        """Signal the run loop to stop claiming and drain. Idempotent.

        Called by the SIGTERM/SIGINT handlers (and directly by tests). Safe to
        call repeatedly — a second signal during drain is absorbed.
        """
        if not self._draining.is_set():
            _log.info("drain requested", worker_id=self._worker_id)
        self._draining.set()

    async def run_once(
        self, *, lease_seconds: int = MEDIUM_LEASE.lease_seconds, batch: int = 1
    ) -> int:
        """Claim up to ``batch`` due jobs and execute each. Returns the count run.

        The claim is a short committed transaction (T3); each job then executes
        through the RLS choke point (T4). Per-job-type lease refinement (heartbeat
        to the type's lease) and bounded async concurrency land in T5.
        """
        records = self._queue.claim(
            worker_id=self._worker_id, lease_seconds=lease_seconds, limit=batch
        )
        for record in records:
            await self._executor.execute(record)
        return len(records)

    async def run(self, *, install_signal_handlers: bool = True) -> None:
        """Run the continuous claim→execute loop until a drain signal, then drain.

        Each iteration claims only as many jobs as there are free concurrency
        slots and dispatches each as a background task, so at most ``concurrency``
        jobs run at once (D-A0-3). When nothing is due, it waits a jittered poll
        interval (so N workers don't thunder the claim query in lockstep) — but a
        drain signal wakes it immediately. On drain: stop claiming, let in-flight
        jobs finish within the drain bound, then exit; anything still running is
        cancelled and left for lease-expiry reclaim (D-A0-5).

        Args:
            install_signal_handlers: Whether this worker owns the PROCESS's
                SIGTERM/SIGINT (D-A0-5). ``True`` (default) is the standalone
                worker-process posture: a signal requests a graceful drain.
                In-process hosting (the single-uvicorn deploy — D-08-5) MUST
                pass ``False``: uvicorn owns process signals, and asyncio's
                ``loop.add_signal_handler`` REPLACES the process-level handler
                uvicorn installed via ``signal.signal`` — so a worker that
                traps signals in uvicorn's process swallows ^C (the drain runs
                but the server never shuts down — R9-004). In-process, the
                drain path is the app lifespan: :meth:`InProcessWorker.aclose`
                → :meth:`request_drain` → await this loop.
        """
        if install_signal_handlers:
            self._install_signal_handlers()
        _log.info(
            "worker loop started",
            worker_id=self._worker_id,
            concurrency=self._concurrency,
        )
        while not self._draining.is_set():
            # R9-093: stamp BEFORE the sweeps, so the beat proves the loop is
            # turning even while a slow sweep runs. Stamping after would let a
            # long-but-healthy sweep look like a stall.
            self._last_beat_at = datetime.now(UTC)
            self._maybe_run_maintenance()
            self._maybe_run_scheduler_tick()
            await self._maybe_run_catalog_sync()
            await self._maybe_run_skill_catalog_sync()
            await self._maybe_run_initiative_provisioner()
            await self._maybe_run_approval_sweep()
            await self._maybe_run_dead_leg_sweep()
            free = self._concurrency - len(self._in_flight)
            # Claim ONE at a time (not a batch of ``free``): the fairness count is
            # evaluated against committed state, so a batch would let all its
            # candidates pass the per-user check at once and over-grab a single
            # user. One-at-a-time re-evaluates the cap per claim — exact, no
            # starvation — and the loop still fills to ``concurrency`` by iterating.
            try:
                records = (
                    self._queue.claim(
                        worker_id=self._worker_id,
                        lease_seconds=self._claim_lease_seconds,
                        limit=1,
                        max_per_user=self._max_jobs_per_user,
                        max_global=self._max_jobs_global,
                    )
                    if free > 0
                    else []
                )
            except Exception:  # noqa: BLE001 — a claim failure must not crash the worker loop
                _log.exception("job claim failed", worker_id=self._worker_id)
                if self._draining.is_set():
                    # R9-043: a DB error during drain (e.g. a transient blip mid
                    # Ctrl-C shutdown) trivially satisfies "stop claiming new
                    # work" — end the drain loop cleanly instead of retrying
                    # against a DB that just dropped the shutdown out from under
                    # it. In-flight jobs still drain normally below (R9-004);
                    # only NEW claims are affected.
                    break
                # Not draining: a transient DB error must not spin the loop hot
                # either — back off one poll interval (same jittered cadence as
                # the empty-claim wait below), then retry.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._draining.wait(), timeout=self._next_poll_delay())
                continue
            for record in records:
                task: asyncio.Task[object] = asyncio.create_task(self._executor.execute(record))
                self._in_flight.add(task)
                task.add_done_callback(self._settle_job_task)
            if not records:
                # Nothing due (or no free slots) — wait a jittered interval, but
                # wake immediately if a drain is requested mid-sleep.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._draining.wait(), timeout=self._next_poll_delay())
        await self._drain()

    def _settle_job_task(self, task: asyncio.Task[object]) -> None:
        """Discard a settled job task, RETRIEVING any escaped exception (R9-013).

        ``JobExecutor.execute`` classifies and records every failure it can; if
        an exception still escapes it, retrieving it here logs an honest ERROR
        instead of asyncio's deferred "Task exception was never retrieved" crash
        report — and the job is left for lease-expiry reclaim, exactly as a hard
        crash would be. (Drain-time ``CancelledError`` is the expected settle.)
        """
        self._in_flight.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.error(
                "job task crashed outside the executor's failure paths; "
                "left for lease-expiry reclaim",
                worker_id=self._worker_id,
                error=str(exc),
            )

    async def _drain(self) -> None:
        """Await in-flight jobs within the drain bound; cancel the rest.

        Jobs that finish within ``drain_seconds`` complete normally. Anything still
        running when the bound elapses is cancelled — its handler stops, its
        heartbeat stops, and its lease lapses so another worker reclaims it after
        the deploy (a deploy-mid-job degrades to a resume, not a loss). The same
        mechanism as a hard crash, plus a clean stop-claiming.
        """
        if not self._in_flight:
            _log.info("drained: nothing in flight", worker_id=self._worker_id)
            return
        _log.info(
            "draining in-flight jobs",
            in_flight=len(self._in_flight),
            drain_seconds=self._drain_seconds,
        )
        pending = self._in_flight
        done, still_running = await asyncio.wait(pending, timeout=self._drain_seconds)
        if still_running:
            _log.warning(
                "drain bound exceeded; cancelling for lease-expiry reclaim",
                cancelled=len(still_running),
                finished=len(done),
            )
            for task in still_running:
                task.cancel()
            # Let each cancelled execute() run its finally (GUC reset) to completion.
            await asyncio.gather(*still_running, return_exceptions=True)
        else:
            _log.info("drained cleanly", finished=len(done))

    def _maybe_run_maintenance(self) -> None:
        """Run the maintenance sweep if its cadence has elapsed (monotonic clock).

        Guarded the same way every other periodic task in this loop is
        (scheduler tick, catalog sync, ...): a DB failure during the sweep is
        logged, never crashes the loop — R9-049, the sibling of R9-043's
        claim() guard (a DB drop during maintenance crashed the same way).
        """
        elapsed = time.monotonic() - self._last_maintenance
        if elapsed >= self._maintenance_interval:
            try:
                self.run_maintenance()
            except Exception:  # noqa: BLE001 — a maintenance failure must not crash the worker loop
                _log.exception("maintenance sweep failed", worker_id=self._worker_id)
            self._last_maintenance = time.monotonic()

    def _maybe_run_scheduler_tick(self) -> None:
        """Run the A1 scheduler tick if wired + its cadence has elapsed (additive).

        A no-op on a plain A0 worker (no tick wired). The tick is itself
        leader-gated (only the advisory-lock holder fires), so every worker may
        call this safely — at most one process actually ticks. Sync DB work, like
        the maintenance sweep; a failure is logged, never crashing the loop.
        """
        if self._scheduler_tick is None:
            return
        if time.monotonic() - self._last_scheduler_tick < self._scheduler_tick_interval:
            return
        try:
            self._scheduler_tick.run_once()
        except Exception:  # noqa: BLE001 — a tick failure must not crash the worker loop
            _log.exception("scheduler tick failed", worker_id=self._worker_id)
        self._last_scheduler_tick = time.monotonic()

    async def _maybe_run_catalog_sync(self) -> None:
        """Run the N2 catalog auto-sync if wired + its (daily-ish) cadence has elapsed.

        A no-op when unwired/disabled (None). Like the scheduler tick it is leader-gated
        (only the catalog-lock holder pulls — N2-D-2), so every worker may call this safely.
        Unlike the tick, the sync does a BLOCKING ``git clone`` + file write, so it is
        offloaded to a thread (``asyncio.to_thread``) — never inline — so a multi-second
        clone never stalls the shared API event loop. A failure is logged, never crashing the
        loop; the last-good mirror is preserved (fail-soft, N2-D-3) and retried next cadence.
        """
        if self._catalog_sync is None:
            return
        if (
            self._last_catalog_sync is not None
            and time.monotonic() - self._last_catalog_sync < self._catalog_sync_interval
        ):
            return
        try:
            await asyncio.to_thread(self._catalog_sync.run_once)
        except Exception:  # noqa: BLE001 — a sync failure must not crash the worker loop
            _log.exception("catalog sync failed", worker_id=self._worker_id)
        self._last_catalog_sync = time.monotonic()

    async def _maybe_run_initiative_provisioner(self) -> None:
        """Run the A5 schedule-provisioning sweep if wired + its cadence elapsed (T10).

        The catalog-sync shape: a no-op when unwired (None); leader-gated on its OWN
        advisory key inside ``run_once``; DB-bound work offloaded to a thread; a failure
        is logged, never crashing the loop (fail-soft; retried next cadence).
        """
        if self._initiative_provisioner is None:
            return
        if (
            self._last_initiative_provision is not None
            and time.monotonic() - self._last_initiative_provision
            < self._initiative_provisioner_interval
        ):
            return
        from datetime import UTC, datetime

        try:
            await asyncio.to_thread(self._initiative_provisioner.run_once, now=datetime.now(UTC))
        except Exception:  # noqa: BLE001 — a sweep failure must not crash the worker loop
            _log.exception("initiative provisioning failed", worker_id=self._worker_id)
        self._last_initiative_provision = time.monotonic()

    async def _maybe_run_approval_sweep(self) -> None:
        """Run the A3 approval reminder/expiry sweep if wired + its cadence has elapsed (T9).

        A no-op when unwired (None). Leader-gated inside ``run_once`` (its own advisory key —
        at most one process sweeps), so every worker may call this safely. The sweep's DB work
        + best-effort C0 voicing are async; a failure is logged, never crashing the loop. The
        state changes (remind-once CAS, terminal expiry + auto-pause) always happen before any
        voice, so a voicing degrade never leaves an approval un-expired.
        """
        if self._approval_sweep is None:
            return
        if (
            self._last_approval_sweep is not None
            and time.monotonic() - self._last_approval_sweep < self._approval_sweep_interval
        ):
            return
        try:
            await self._approval_sweep.run_once(now=datetime.now(UTC))
        except Exception:  # noqa: BLE001 — a sweep failure must not crash the worker loop
            _log.exception("approval sweep failed", worker_id=self._worker_id)
        self._last_approval_sweep = time.monotonic()

    async def _maybe_run_dead_leg_sweep(self) -> None:
        """Run the A3 dead-leg voicing sweep if wired + its cadence has elapsed (T13).

        A no-op when unwired (None). Leader-gated inside ``run_once`` (a DISTINCT advisory key
        from the approval sweep + the scheduler tick), so every worker may call it safely. Parks
        each retry-exhausted task ``active → waiting(on_user)`` + voices its StuckReport
        best-effort; a failure is logged, never crashing the loop.
        """
        if self._dead_leg_sweep is None:
            return
        if (
            self._last_dead_leg_sweep is not None
            and time.monotonic() - self._last_dead_leg_sweep < self._dead_leg_sweep_interval
        ):
            return
        try:
            await self._dead_leg_sweep.run_once(now=datetime.now(UTC))
        except Exception:  # noqa: BLE001 — a sweep failure must not crash the worker loop
            _log.exception("dead-leg sweep failed", worker_id=self._worker_id)
        self._last_dead_leg_sweep = time.monotonic()

    async def _maybe_run_skill_catalog_sync(self) -> None:
        """Run the S2 skill-catalog auto-sync if wired + its (daily-ish) cadence has elapsed.

        The exact N2 ``_maybe_run_catalog_sync`` shape (S2-D-1, reuse): a no-op when
        unwired/disabled (None); leader-gated via a DISTINCT advisory key so it never entangles
        with the MCP catalog-sync leadership; the blocking clone + write is offloaded to a thread;
        a failure is logged, never crashing the loop (fail-soft — last-good mirror preserved).
        """
        if self._skill_catalog_sync is None:
            return
        if (
            self._last_skill_catalog_sync is not None
            and time.monotonic() - self._last_skill_catalog_sync < self._skill_catalog_sync_interval
        ):
            return
        try:
            await asyncio.to_thread(self._skill_catalog_sync.run_once)
        except Exception:  # noqa: BLE001 — a sync failure must not crash the worker loop
            _log.exception("skill catalog sync failed", worker_id=self._worker_id)
        self._last_skill_catalog_sync = time.monotonic()

    def run_maintenance(self) -> None:
        """Rescuer + cleaner + retention sweep (D-A0-4). Idempotent; safe per-worker.

        Reclaims expired leases (crashed/drained workers' jobs), ages terminal jobs
        older than ``archive_after`` into the cold archive, and purges archive rows
        past retention. Each worker runs this on its own cadence; the operations are
        idempotent and bounded (A1 may add single-leader election to dedupe).
        """
        now = datetime.now(UTC)
        reclaimed = self._queue.reclaim_expired(now=now)
        archived = self._queue.archive_terminal(
            older_than=now - timedelta(seconds=self._archive_after)
        )
        purged = self._queue.purge_archive(
            older_than=now - timedelta(seconds=self._archive_retention)
        )
        if reclaimed or archived or purged:
            _log.info(
                "maintenance sweep",
                worker_id=self._worker_id,
                reclaimed=reclaimed,
                archived=archived,
                purged=purged,
            )

    def _next_poll_delay(self) -> float:
        """Jittered poll interval: ``interval + U(0, jitter)`` (D-A0-3)."""
        return self._poll_interval + secrets.randbelow(1000) / 1000 * self._poll_jitter

    def _install_signal_handlers(self) -> None:
        """Trap SIGTERM/SIGINT → ``request_drain``. No-op where unsupported."""
        loop = asyncio.get_running_loop()
        for sig in _DRAIN_SIGNALS:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self.request_drain)

    def livez(self) -> tuple[str, int]:
        """DB-free liveness — the process is up. Mirrors the api ``/livez``."""
        return ("ok", 200)

    def healthz(self) -> tuple[str, int]:
        """Readiness — BOTH engines can reach Postgres (200) or not (503).

        Probes the dispatch engine AND the RLS engine: a worker whose RLS engine
        is unreachable can claim jobs but not run handlers, which is not ready.
        """
        for engine in (self._dispatch_engine, self._rls_engine):
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            except Exception:  # noqa: BLE001 — health probe reports, never raises
                _log.warning("worker healthz: an engine is unreachable")
                return ("db_unreachable", 503)
        return ("ok", 200)

    def aclose(self) -> None:
        """Dispose both engines (shutdown). Idempotent."""
        self._dispatch_engine.dispose()
        self._rls_engine.dispose()


def build_worker(
    config: APIConfig,
    registry: JobRegistry,
    *,
    scheduler_tick_builder: Callable[[Engine, Engine], SchedulerTick] | None = None,
    catalog_sync_builder: Callable[[Engine], CatalogSyncTask | None] | None = None,
    skill_catalog_sync_builder: Callable[[Engine], SkillCatalogSyncTask | None] | None = None,
    initiative_provisioner_builder: Callable[[Engine, Engine], InitiativeProvisioner | None]
    | None = None,
    approval_sweep_builder: Callable[[Engine, Engine], ApprovalSweepRunner | None] | None = None,
    dead_leg_sweep_builder: Callable[[Engine, Engine], DeadLegSweeper | None] | None = None,
) -> Worker:
    """Compose a :class:`Worker` from config — the worker's composition root.

    Mirrors the api lifespan's engine wiring: the cross-tenant **dispatch** engine
    from ``worker_dispatch_database_url`` (falling back to the superuser
    ``database_url`` for v0.1), and the ``persona_app`` **RLS** engine from
    ``app_database_url`` (falling back to ``database_url``). Fail-fast if no DSN
    is configured — a worker with no database is a misconfiguration, not a
    degraded mode.

    ``scheduler_tick_builder`` is A1's additive composition seam (D-A1-X-worker-
    additive): a callback receiving ``(dispatch_engine, rls_engine)`` that returns
    the leader-gated :class:`SchedulerTick`. ``None`` (a plain A0 worker) wires no
    tick — the worker behaves exactly as A0 shipped. The builder is invoked AFTER
    the engines are created, so the tick shares the worker's two engines.
    """
    dispatch_url = config.worker_dispatch_database_url or config.database_url
    if not dispatch_url:
        msg = "worker requires DATABASE_URL (or WORKER_DISPATCH_DATABASE_URL) to be set"
        raise ValueError(msg)
    # The RLS engine MUST be the non-superuser persona_app role — a superuser
    # connection bypasses RLS entirely, silently negating the tenant boundary for
    # every handler. Spec R2 R2-D-2: the worker is the SECOND call site of the
    # cloud-config fail-fast — a misconfigured worker running as superuser is the
    # same hole as the API path (F-06), so it REFUSES to start (was a soft WARN).
    # The pure-config asserts run here before the engine is built; the is_superuser
    # probe runs once the rls_engine exists (community: never gated).
    check_cloud_config_guard(config)
    dispatch_engine = create_db_engine(dispatch_url)
    rls_engine = make_rls_engine(config.effective_app_database_url)
    check_cloud_config_guard(config, probe_engine=rls_engine)
    scheduler_tick = (
        scheduler_tick_builder(dispatch_engine, rls_engine)
        if scheduler_tick_builder is not None
        else None
    )
    # N2 catalog auto-sync — additive, leader-gated, built on the cross-tenant dispatch
    # engine (its advisory lock is not tenant data). None when unwired or disabled.
    catalog_sync = (
        catalog_sync_builder(dispatch_engine) if catalog_sync_builder is not None else None
    )
    # S2 skill-catalog auto-sync — additive, leader-gated (distinct key), built on the same
    # cross-tenant dispatch engine. None when unwired or disabled.
    skill_catalog_sync = (
        skill_catalog_sync_builder(dispatch_engine)
        if skill_catalog_sync_builder is not None
        else None
    )
    _log.info(
        "worker composition root built",
        dispatch_role_dedicated=bool(config.worker_dispatch_database_url),
        rls_role_superuser_fallback=not bool(config.app_database_url),
        registered_types=len(registry.types()),
        scheduler_tick_wired=scheduler_tick is not None,
        catalog_sync_wired=catalog_sync is not None,
    )
    initiative_provisioner = (
        initiative_provisioner_builder(dispatch_engine, rls_engine)
        if initiative_provisioner_builder is not None
        else None
    )
    # A3 lifecycle sweeps — additive, each leader-gated on its own advisory key, built on the
    # worker's two engines (dispatch for the leader session + cross-tenant scans; RLS for the
    # owner-scoped store actions + persona-tag voicing). None when unwired → the loop is unchanged.
    approval_sweep = (
        approval_sweep_builder(dispatch_engine, rls_engine)
        if approval_sweep_builder is not None
        else None
    )
    dead_leg_sweep = (
        dead_leg_sweep_builder(dispatch_engine, rls_engine)
        if dead_leg_sweep_builder is not None
        else None
    )
    return Worker(
        dispatch_engine=dispatch_engine,
        rls_engine=rls_engine,
        registry=registry,
        scheduler_tick=scheduler_tick,
        scheduler_tick_interval_seconds=config.scheduler_tick_interval_seconds,
        catalog_sync=catalog_sync,
        catalog_sync_interval_seconds=config.mcp_catalog_sync_interval_seconds,
        skill_catalog_sync=skill_catalog_sync,
        skill_catalog_sync_interval_seconds=config.skill_catalog_sync_interval_seconds,
        initiative_provisioner=initiative_provisioner,
        approval_sweep=approval_sweep,
        approval_sweep_interval_seconds=config.approval_sweep_interval_seconds,
        dead_leg_sweep=dead_leg_sweep,
        dead_leg_sweep_interval_seconds=config.dead_leg_sweep_interval_seconds,
        concurrency=config.worker_concurrency,
        poll_interval_seconds=config.worker_poll_interval_seconds,
        poll_jitter_seconds=config.worker_poll_jitter_seconds,
        claim_lease_seconds=config.worker_claim_lease_seconds,
        drain_seconds=config.worker_drain_seconds,
        max_jobs_per_user=config.worker_max_jobs_per_user,
        max_jobs_global=config.worker_max_jobs_global,
        maintenance_interval_seconds=config.worker_maintenance_interval_seconds,
        archive_after_seconds=config.worker_archive_after_seconds,
        archive_retention_seconds=config.worker_archive_retention_seconds,
    )
