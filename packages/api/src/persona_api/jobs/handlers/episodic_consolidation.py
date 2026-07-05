"""The episodic-consolidation job handler — K8's sleep-time engine as an A0 tenant.

The exact shape of K7's ``graph_consolidation`` tenant (the ratified trigger
pattern, K8-D-8): enqueued from the synthesis tail (the moment this persona's
memory got dirty), coalesced by watermark bucket, deferred to the idle
boundary, owner-scoped through the worker's RLS chokepoint. The engine itself
(``persona.stores.engine``) is deterministic and idempotent — a re-delivery
re-runs a pass that converges to zero new writes — the second line behind
A0's ``ON CONFLICT`` dedup.

Gating (the built-but-inert guard): the handler is registered AND the
synthesis-tail trigger fires only when ``PERSONA_EPISODIC_ENGINE_ENABLED``
(the ``EpisodicSettings.engine_enabled`` kill switch) is on. Engine failure is
fail-soft by design: no gists ⇒ recall over raw chunks; a turn is never
touched (the engine exists entirely off the turn path).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.jobs import MEDIUM_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.jobs import JobContext, JobRegistry
    from persona.stores.engine import EpisodicConsolidationReport

    from persona_api.jobs.queue import JobQueue

__all__ = [
    "EPISODIC_CONSOLIDATION_JOB_TYPE",
    "EpisodicConsolidationHandler",
    "EpisodicConsolidationJobPayload",
    "EpisodicEngineRunner",
    "enqueue_episodic_consolidation",
    "episodic_consolidation_idempotency_key",
    "register_episodic_consolidation_handler",
]

EPISODIC_CONSOLIDATION_JOB_TYPE = "episodic_consolidation"

_logger = get_logger("jobs.episodic_consolidation")


class EpisodicConsolidationJobPayload(JobPayload):
    """Which persona + idle-window bucket this run coalesces to.

    ``watermark_bucket`` scopes the A0 idempotency key so a burst of turn-tail
    enqueues within one bucket collapses to a single run (the D-K2-2 pattern);
    the engine derives its real watermark from gist coverage at run time.
    """

    persona_id: str
    watermark_bucket: str


def episodic_consolidation_idempotency_key(payload: EpisodicConsolidationJobPayload) -> str:
    """``episodic_consolidation:{persona}:{bucket}`` — one run per persona per bucket."""
    return f"episodic_consolidation:{payload.persona_id}:{payload.watermark_bucket}"


@runtime_checkable
class EpisodicEngineRunner(Protocol):
    """The core engine port (``persona.stores.engine.EpisodicConsolidationEngine``)."""

    async def run(self, owner_id: str, persona_id: str) -> EpisodicConsolidationReport: ...


class EpisodicConsolidationHandler:
    """Owner-scoped engine run: cluster → gists + graph candidates, metered."""

    def __init__(self, *, engine: EpisodicEngineRunner) -> None:
        self._engine = engine

    async def handle(self, payload: EpisodicConsolidationJobPayload, context: JobContext) -> None:
        # The engine is async end-to-end (summarizer awaits; the sync graph
        # merge is to_thread'd inside) — the RLS contextvar the executor bound
        # propagates through both, so every write stays owner-scoped.
        report = await self._engine.run(context.owner_id, payload.persona_id)
        _logger.info(
            "episodic_consolidation ran",
            owner_id=context.owner_id,
            persona_id=payload.persona_id,
            windows=report.windows_formed,
            gists=report.gists_written,
            candidates=report.candidates_emitted,
            skipped=len(report.skipped),
        )
        context.meter(
            amount_micros=0,
            kind="model",
            detail={
                "surface": "episodic_consolidation",
                "windows": str(report.windows_formed),
                "gists": str(report.gists_written),
                "candidates": str(report.candidates_emitted),
                "skipped": str(len(report.skipped)),
            },
        )


def register_episodic_consolidation_handler(
    registry: JobRegistry, *, engine: EpisodicEngineRunner
) -> None:
    """Register the sleep-time engine tenant (Spec K8, K8-D-8)."""
    registry.register(
        JobTypeSpec(
            type=EPISODIC_CONSOLIDATION_JOB_TYPE,
            payload_model=EpisodicConsolidationJobPayload,
            handler=EpisodicConsolidationHandler(engine=engine),
            idempotency_key=episodic_consolidation_idempotency_key,
            retry=RetryPolicy(max_attempts=2),
            lease=MEDIUM_LEASE,
        )
    )


def enqueue_episodic_consolidation(
    queue: JobQueue,
    *,
    owner_id: str,
    persona_id: str,
    delay_seconds: float,
    bucket_seconds: float,
    now: datetime | None = None,
) -> None:
    """Enqueue a coalesced engine run (the synthesis-tail/turn-tail trigger).

    ``watermark_bucket`` coalesces a burst to one run per persona;
    ``scheduled_at`` defers it to the idle boundary. An identical re-enqueue
    within the bucket is A0's ``ON CONFLICT`` no-op. Off the critical path.
    """
    moment = now or datetime.now(UTC)
    bucket = str(int(moment.timestamp() // bucket_seconds)) if bucket_seconds > 0 else "0"
    payload = EpisodicConsolidationJobPayload(persona_id=persona_id, watermark_bucket=bucket)
    queue.enqueue(
        type=EPISODIC_CONSOLIDATION_JOB_TYPE,
        owner_id=owner_id,
        payload=payload.model_dump(),
        idempotency_key=episodic_consolidation_idempotency_key(payload),
        scheduled_at=moment + timedelta(seconds=delay_seconds),
    )
