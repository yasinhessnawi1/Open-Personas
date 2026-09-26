"""The quiet-hours deferred flush: held initiative released when the window ends (R9-237).

The held batch is released by the daily scan fire (R9-183, flush-on-next-scan),
which fires at the scan hour in the user's zone. A quiet window can cover that
hour every day (22:00 to 08:00 covers 07:00), so a flush that only held inside
quiet hours would never deliver and every notice would age out. The pipeline
therefore hands a quiet-hours flush to :class:`QueueDeferredFlushScheduler`,
which enqueues ONE durable A0 job for the window's end:

    scan fire at 07:00 (inside quiet) → pipeline.flush → nothing delivered
    → enqueue ``initiative_deferred_flush`` due at 08:00 local (as UTC)
    → claim → owner GUC → :class:`InitiativeDeferredFlushHandler`
    → pause gate → dial gate → pipeline.flush (re-checks quiet hours) → delivery.

Idempotent per owner per release instant: every persona's scan fire of the same
morning asks for the same instant, and the A0 ``(owner_id, idempotency_key)``
unique collapses them to one job. The handler repeats the scan fire's gates (the
owner's autonomy pause, then the dial) because it spends (the grounding re-run)
and can speak, and bills that spend the way the scan fire bills its own flush.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.initiative import InitiativeDial
from persona.jobs import MEDIUM_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from pydantic import AwareDatetime, field_validator

from persona_api.approvals import AutonomyPauseCheck, never_paused
from persona_api.services.background_billing import bill_background_llm
from persona_api.services.llm_usage_collector import collect_llm_usage

if TYPE_CHECKING:
    from collections.abc import Collection

    from persona.jobs import JobContext, JobRegistry
    from persona_runtime.cost import CostSource
    from sqlalchemy import Engine

    from persona_api.editions.credits_policy import CreditsPolicy
    from persona_api.initiative.handler import HeldBatchFlush
    from persona_api.jobs.queue import JobQueue

__all__ = [
    "INITIATIVE_DEFERRED_FLUSH_JOB_TYPE",
    "DeferredFlushPayload",
    "InitiativeDeferredFlushHandler",
    "QueueDeferredFlushScheduler",
    "deferred_flush_idempotency_key",
    "register_initiative_deferred_flush_handler",
]

INITIATIVE_DEFERRED_FLUSH_JOB_TYPE = "initiative_deferred_flush"
_SURFACE = "initiative_deferred_flush"

_log = get_logger("api.initiative.deferred_flush")


class DeferredFlushPayload(JobPayload):
    """The instant the owner's quiet window ends (the job's ``scheduled_at``).

    Normalised to UTC on the way in: the idempotency and billing keys are built
    from its ISO text, so one instant must have one spelling whatever offset
    the caller handed over.
    """

    release_at: AwareDatetime

    @field_validator("release_at")
    @classmethod
    def _to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


def deferred_flush_idempotency_key(payload: DeferredFlushPayload) -> str:
    """One job per owner per release instant (the owner half is the A0 unique's other column)."""
    return f"{INITIATIVE_DEFERRED_FLUSH_JOB_TYPE}:{payload.release_at.isoformat()}"


class QueueDeferredFlushScheduler:
    """The runtime ``DeferredFlushScheduler`` over the A0 queue."""

    def __init__(self, queue: JobQueue) -> None:
        """Inject the owner-scoped queue (the worker's RLS engine)."""
        self._queue = queue

    def schedule_flush(self, owner_id: str, *, release_at: datetime) -> None:
        """Enqueue the deferred flush due at ``release_at``; a duplicate is A0's no-op."""
        payload = DeferredFlushPayload(release_at=release_at)
        self._queue.enqueue(
            type=INITIATIVE_DEFERRED_FLUSH_JOB_TYPE,
            owner_id=owner_id,
            payload=payload.model_dump(mode="json"),
            idempotency_key=deferred_flush_idempotency_key(payload),
            scheduled_at=payload.release_at,
        )


class InitiativeDeferredFlushHandler:
    """Pause gate → dial gate → flush → bill. Every failure path is silence."""

    def __init__(
        self,
        *,
        held_batch: HeldBatchFlush,
        held_personas: Callable[[str], Collection[str]],
        dial_reader: Callable[[str, str], InitiativeDial],
        pause_check: AutonomyPauseCheck = never_paused,
        credits_policy: CreditsPolicy | None = None,
        rls_engine: Engine | None = None,
        cost_source: CostSource | None = None,
        floor: int = 1,
    ) -> None:
        """Inject the pipeline flush and the scan fire's gates.

        ``held_personas`` names the personas whose notices are held for the
        owner: the dial gate passes when at least one of them is not OFF, the
        owner-wide form of the scan fire's per-persona dial exit (the flush is
        owner-wide; a notice whose own persona is OFF still stays held inside
        it, as on the scan fire). The billing arguments mirror
        :class:`~persona_api.initiative.handler.InitiativeScanHandler`.
        """
        self._held_batch = held_batch
        self._held_personas = held_personas
        self._dial_reader = dial_reader
        self._pause_check = pause_check
        self._credits_policy = credits_policy
        self._rls_engine = rls_engine
        self._cost_source = cost_source
        self._floor = floor

    async def handle(self, payload: DeferredFlushPayload, context: JobContext) -> None:
        """One deferred flush; never raises (silence is the safe state)."""
        owner_id = context.owner_id
        if self._pause_check(owner_id):
            _log.info("owner autonomy paused; deferred initiative flush exits")
            return
        if not self._any_dial_on(owner_id):
            _log.info("no held persona has initiative on; deferred flush exits")
            return
        with collect_llm_usage() as usage:
            try:
                await self._held_batch.flush(owner_id)
            except Exception:  # noqa: BLE001, the job must not retry into a second send
                _log.warning("deferred initiative flush failed; degrading to silence")
        totals = usage.totals()
        bill_background_llm(
            credits_policy=self._credits_policy,
            rls_engine=self._rls_engine,
            owner_id=owner_id,
            provider=totals.provider,
            model=totals.model,
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            cost_usd=totals.cost_usd,
            surface=_SURFACE,
            billing_key=f"{_SURFACE}:{owner_id}:{payload.release_at.isoformat()}",
            cost_source=self._cost_source,
            floor=self._floor,
        )
        context.meter(
            amount_micros=0,
            kind="model",
            detail={"surface": _SURFACE, "release_at": payload.release_at.isoformat()},
        )

    def _any_dial_on(self, owner_id: str) -> bool:
        return any(
            self._dial_reader(owner_id, persona_id) is not InitiativeDial.OFF
            for persona_id in set(self._held_personas(owner_id))
        )


def register_initiative_deferred_flush_handler(
    registry: JobRegistry, *, handler: InitiativeDeferredFlushHandler
) -> None:
    """Register the deferred-flush tenant (worker root, next to the scan tenant)."""
    registry.register(
        JobTypeSpec(
            type=INITIATIVE_DEFERRED_FLUSH_JOB_TYPE,
            payload_model=DeferredFlushPayload,
            handler=handler,
            idempotency_key=deferred_flush_idempotency_key,
            retry=RetryPolicy(max_attempts=2),
            lease=MEDIUM_LEASE,
        )
    )
