"""Job execution + the RLS-GUC choke point (Spec A0, T4).

:class:`JobExecutor` runs one claimed job: it transitions ``claimed → running``
on the dispatch engine, then dispatches to the registered handler **inside the
one structural choke point** that binds the job owner's RLS scope, and finally
records the outcome. The choke point is the single, non-bypassable place where
tenant identity is established for execution:

    token = current_user_id.set(record.owner_id)   # bind owner scope
    try:
        ... build owner-scoped context; run handler ...
    finally:
        current_user_id.reset(token)                # always release

The handler receives a :class:`WorkerJobContext` whose only DB affordance is an
owner-scoped ``persona_app`` connection. The dispatch engine (cross-tenant) lives
in the worker loop and is never reachable from a handler. Setting the contextvar
*also* scopes any collaborator built on the shared RLS engine (the runtime stack
a future persona-leg handler drives), so both the direct-DB and collaborator
paths are owner-bound by the same one act — belt and suspenders.

Failure policy (T6): a :class:`~persona.errors.PermanentJobError` from the handler
terminates the job as ``failed`` (non-retryable). Any other exception is transient
— the job retries with capped-exponential backoff (jittered) until the type's
``max_attempts`` is reached, then dead-letters as ``dead`` with the recorded
cause. A worker that *crashes* mid-job (no exception fires) is the lease-expiry
reclaim path; the crash-loop cap dead-letters a job re-claimed past its attempts
so neither failure mode storms (criterion 5). A drain cancellation re-raises and
leaves the job for reclaim (T5). A **pre-dispatch poison** — a job type this
process has no handler for, or a durable payload that fails its model's
validation — can never succeed by retrying, so it dead-letters immediately
(R9-013; the pre-fix escape crashed the asyncio task unretrieved and
lease-cycled forever).
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from persona.errors import PermanentJobError, UnknownJobTypeError
from persona.jobs import JobState
from persona.logging import get_logger

from persona_api.jobs.context import WorkerJobContext
from persona_api.middleware.rls_context import current_user_id

if TYPE_CHECKING:
    from persona.jobs import JobRegistry, JobTypeSpec, LeasePolicy, RetryPolicy
    from sqlalchemy import Engine

    from persona_api.jobs.queue import JobQueue, JobRecord

__all__ = ["JobExecutor"]

_log = get_logger("api.jobs.executor")


def _equal_jitter(delay: float) -> float:
    """Equal-jitter a backoff delay: ``delay/2 + U(0, delay/2)`` → ``[delay/2, delay]``.

    Spreads a cohort of jobs that failed together so their retries don't all fire
    at the same instant (the thundering-herd / retry-storm mitigation), while
    keeping the delay within a factor of 2 of the policy's intended backoff.
    """
    half = delay / 2
    return half + (secrets.randbelow(1000) / 1000) * half


class JobExecutor:
    """Executes one claimed job through the RLS choke point.

    Args:
        queue: The durable queue (on the dispatch engine) for lifecycle writes.
        registry: The typed handler registry (resolves type → handler + payload).
        rls_engine: The ``persona_app`` RLS engine handlers' owner-scoped
            connections are built on — NOT the dispatch engine.
        worker_id: This worker's identity (lease ownership checks).
    """

    def __init__(
        self,
        *,
        queue: JobQueue,
        registry: JobRegistry,
        rls_engine: Engine,
        worker_id: str,
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._rls_engine = rls_engine
        self._worker_id = worker_id

    async def execute(self, record: JobRecord) -> JobState:
        """Run one claimed job; return its terminal-or-failed state.

        Returns ``SUCCEEDED`` if the handler completed and the job was marked
        done, ``FAILED`` if the handler raised (the job is left for lease-expiry
        reclaim) or the job could not be moved to ``running``. A drain-time
        cancellation propagates (``CancelledError``) — the job is left claimed so
        its lease lapses and another worker reclaims it post-deploy.
        """
        if not self._queue.mark_running(job_id=record.id, worker_id=self._worker_id):
            # Lost the lease between claim and run (reclaimed/completed elsewhere).
            _log.warning("job not runnable; lease lost", job_id=record.id, type=record.type)
            return JobState.FAILED

        # Pre-dispatch poison guard (R9-013): a job whose TYPE this process cannot
        # resolve can never succeed here — retrying re-crashes, the lease lapses,
        # reclaim re-claims, forever (the "Task exception was never retrieved"
        # poison loop). Fail it DECISIVELY: dead-letter with the honest cause,
        # release the claim, and let nothing escape the task.
        try:
            spec = self._registry.get(record.type)
        except UnknownJobTypeError as exc:
            return self._dead_letter_poisoned(record, exc, reason="unknown job type")

        # Crash-loop cap: a job re-claimed (via lease-expiry reclaim) more times than
        # the policy allows dead-letters WITHOUT running again. Deterministic
        # handler failures are capped on the failure path below; this catches the
        # case where the worker keeps DYING mid-job (no handler exception ever
        # fires) — without it, such a job would reclaim-loop forever (criterion 5).
        if record.attempt > spec.retry.max_attempts:
            self._queue.mark_dead(
                job_id=record.id, worker_id=self._worker_id, error="max attempts exceeded"
            )
            _log.warning("job dead-lettered: crash-loop cap", job_id=record.id, type=record.type)
            return JobState.DEAD

        # Same poison class (R9-013): the payload is durable, so a payload that
        # fails its registered model's validation fails it on EVERY delivery —
        # deterministic, never retryable in this or any process. Dead-letter.
        try:
            payload = self._registry.parse_payload(record.type, record.payload)
        except Exception as exc:  # noqa: BLE001 — pure validation over stored JSONB; deterministic
            return self._dead_letter_poisoned(record, exc, reason="payload parse failed")
        context = WorkerJobContext(
            owner_id=record.owner_id,
            rls_engine=self._rls_engine,
            job_id=record.id,
            job_type=record.type,
        )

        # THE CHOKE POINT — bind the job owner's RLS scope for the whole handler
        # run, release it unconditionally afterwards. Nothing executes the handler
        # outside this binding.
        token = current_user_id.set(record.owner_id)
        try:
            await self._run_handler_with_heartbeat(
                record, spec.handler, payload, context, spec.lease
            )
        except asyncio.CancelledError:
            # Drain/shutdown interrupted the handler. Leave the job claimed; its
            # lease (no longer heartbeated) expires → reclaim re-queues it. Re-raise
            # so the task is truly cancelled — never fall through to complete().
            _log.info("job cancelled mid-flight (drain)", job_id=record.id, type=record.type)
            raise
        except PermanentJobError as exc:
            # The handler declared this failure permanent (non-retryable) → terminal.
            self._queue.mark_failed(job_id=record.id, worker_id=self._worker_id, error=str(exc))
            _log.warning("job permanently failed", job_id=record.id, type=record.type)
            return JobState.FAILED
        except Exception as exc:
            return self._fail_retryable(record, spec, exc)
        finally:
            current_user_id.reset(token)

        if self._queue.complete(job_id=record.id, worker_id=self._worker_id):
            _log.info("job completed", job_id=record.id, type=record.type)
            return JobState.SUCCEEDED
        _log.warning("job finished but completion lost the lease", job_id=record.id)
        return JobState.FAILED

    def _dead_letter_poisoned(self, record: JobRecord, exc: Exception, *, reason: str) -> JobState:
        """Dead-letter a pre-dispatch poison job — terminal ``dead``, no retry cycles.

        The failure fired before the handler could even be resolved (unknown job
        type in this process, undecodable durable payload), so a retry can never
        succeed: the pre-fix behaviour let the exception escape the asyncio task
        ("Task exception was never retrieved"), the claimed job's lease lapsed,
        and reclaim re-crashed it every cycle (R9-013). The job is RUNNING under
        our lease here (``mark_running`` already succeeded), so ``mark_dead``
        releases the claim in the same established transition the retry-exhaustion
        and crash-loop-cap paths use.
        """
        self._queue.mark_dead(job_id=record.id, worker_id=self._worker_id, error=str(exc))
        _log.warning(
            "job dead-lettered: pre-dispatch poison",
            job_id=record.id,
            type=record.type,
            reason=reason,
        )
        return JobState.DEAD

    def _fail_retryable(
        self, record: JobRecord, spec: JobTypeSpec[Any], exc: Exception
    ) -> JobState:
        """A transient handler failure: dead-letter if exhausted, else schedule a retry.

        Exhaustion (``attempt >= max_attempts``) dead-letters durably with the
        cause (criterion 5). Otherwise the job returns to ``queued`` due at
        ``now + backoff``, where backoff is the policy's capped exponential
        (:meth:`RetryPolicy.backoff_for`) with equal jitter applied to spread a
        cohort of co-failing jobs (no synchronized retry storm).
        """
        policy: RetryPolicy = spec.retry
        if policy.is_exhausted(record.attempt):
            self._queue.mark_dead(job_id=record.id, worker_id=self._worker_id, error=str(exc))
            _log.warning(
                "job dead-lettered: retries exhausted",
                job_id=record.id,
                type=record.type,
                attempt=record.attempt,
            )
            return JobState.DEAD
        delay = _equal_jitter(policy.backoff_for(record.attempt))
        scheduled_at = datetime.now(UTC) + timedelta(seconds=delay)
        self._queue.retry(
            job_id=record.id,
            worker_id=self._worker_id,
            error=str(exc),
            scheduled_at=scheduled_at,
        )
        _log.info(
            "job retry scheduled",
            job_id=record.id,
            type=record.type,
            attempt=record.attempt,
            delay_seconds=round(delay, 3),
        )
        return JobState.QUEUED

    async def _run_handler_with_heartbeat(
        self,
        record: JobRecord,
        handler: object,
        payload: object,
        context: WorkerJobContext,
        lease: LeasePolicy,
    ) -> None:
        """Run the handler while a background task renews its lease (D-A0-1).

        The heartbeat extends the lease at ``lease.heartbeat_seconds`` so a live
        long job is never falsely reclaimed; it stops the moment the handler
        finishes, fails, or is cancelled (drain) — after which the lease lapses
        and reclaim takes over. The handler's exception (incl. ``CancelledError``)
        always propagates; the heartbeat is always torn down.
        """
        stop = asyncio.Event()
        beat = asyncio.create_task(self._heartbeat(record.id, lease, stop))
        try:
            # ``handler`` is the registered JobHandler; the Protocol's typed call.
            await handler.handle(payload, context)  # type: ignore[attr-defined]
        finally:
            stop.set()  # ask the heartbeat to finish gracefully...
            beat.cancel()  # ...and hard-stop it if it is mid-sleep/mid-DB-call.
            # Suppress only the heartbeat task's OWN CancelledError — the handler's
            # in-flight exception (if any) remains and re-propagates after finally.
            with contextlib.suppress(asyncio.CancelledError):
                await beat

    async def _heartbeat(self, job_id: str, lease: LeasePolicy, stop: asyncio.Event) -> None:
        """Renew ``job_id``'s lease every ``lease.heartbeat_seconds`` until stopped."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=lease.heartbeat_seconds)
                return  # stop signalled — handler finished
            except TimeoutError:
                renewed = self._queue.heartbeat(
                    job_id=job_id,
                    worker_id=self._worker_id,
                    lease_seconds=lease.lease_seconds,
                )
                if not renewed:
                    # Lost the lease (already reclaimed/completed) — stop beating.
                    return
