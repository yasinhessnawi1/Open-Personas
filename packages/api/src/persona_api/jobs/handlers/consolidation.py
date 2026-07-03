"""The graph-consolidation job handler — A0's durable consolidation tenant (Spec K7, T5).

Runs :meth:`persona.graph.ConsolidationPass.run` as a durable, idempotent A0 job,
enqueued from the SYNTHESIS job's tail (the moment the graph got dirty — K7-D-5). The
owner GUC is bound at the claim→execute chokepoint by the worker's executor (the
``current_user_id`` contextvar) and the ``persona_app`` RLS engine's pool-checkout
listener scopes every connection the pass opens — so the pass is owner-isolated
structurally, not by discipline (D-A0-X-rls-chokepoint).

**Idempotency (declared at registration).** The A0 key is
``graph_consolidation:{watermark-bucket}`` — bursts of synthesis-tail enqueues within
a bucket coalesce to ONE job (D-K2-2 idle-coalescing). A re-delivery re-runs the pass,
which is itself idempotent (a re-scan of an already-consolidated graph merges nothing
and reports ``merged=0``, K7-D-4.6) — the second line behind A0's ``ON CONFLICT`` dedup.

The concrete ``ConsolidationPass`` (transport + index + settings + audit) is composed
at the worker root (T5c); this handler depends only on the ``ConsolidationRunner`` port,
so it is unit-testable with a fake. The pass runs off the event loop
(:func:`asyncio.to_thread`, which propagates the RLS contextvar) so a slow pass never
stalls the worker's other concurrent jobs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.jobs import MEDIUM_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.graph.consolidation import ConsolidationReport
    from persona.jobs import JobContext, JobRegistry

    from persona_api.jobs.queue import JobQueue

__all__ = [
    "GRAPH_CONSOLIDATION_JOB_TYPE",
    "ConsolidationHandler",
    "ConsolidationJobPayload",
    "ConsolidationRunner",
    "consolidation_idempotency_key",
    "enqueue_graph_consolidation",
    "register_graph_consolidation_handler",
]

GRAPH_CONSOLIDATION_JOB_TYPE = "graph_consolidation"

_logger = get_logger("jobs.graph_consolidation")


class ConsolidationJobPayload(JobPayload):
    """Which idle window this consolidation run coalesces to.

    ``watermark_bucket`` is a coarse time bucket (``int(now / bucket_seconds)``) — it
    scopes the A0 idempotency key so a burst of synthesis-tail enqueues within one
    bucket collapses to a single run (D-K2-2). The pass itself reads the live
    per-owner watermark at run time; this value never drives the actual scan.
    """

    watermark_bucket: str


def consolidation_idempotency_key(payload: ConsolidationJobPayload) -> str:
    """``graph_consolidation:{watermark_bucket}`` — coalesces a burst to one run (K7-D-5)."""
    return f"graph_consolidation:{payload.watermark_bucket}"


@runtime_checkable
class ConsolidationRunner(Protocol):
    """The core consolidation pass port (``persona.graph.ConsolidationPass``)."""

    def run(self, owner_id: str) -> ConsolidationReport: ...


class ConsolidationHandler:
    """Idempotent, owner-scoped consolidation: run the pass, meter the summary."""

    def __init__(self, *, pass_: ConsolidationRunner) -> None:
        self._pass = pass_

    async def handle(self, payload: ConsolidationJobPayload, context: JobContext) -> None:  # noqa: ARG002
        # Off the event loop; to_thread propagates the RLS contextvar the executor
        # bound, so the pass's own connections stay owner-scoped.
        report = await asyncio.to_thread(self._pass.run, context.owner_id)
        _logger.info(
            "graph_consolidation ran",
            owner_id=context.owner_id,
            clusters=report.clusters_formed,
            merged=report.nodes_merged,
        )
        context.meter(
            amount_micros=0,
            kind="model",
            detail={
                "surface": "graph_consolidation",
                "clusters": str(report.clusters_formed),
                "merged": str(report.nodes_merged),
                "skipped": str(len(report.skipped)),
            },
        )


def register_graph_consolidation_handler(
    registry: JobRegistry, *, pass_: ConsolidationRunner
) -> None:
    """Register the consolidation handler (A0's durable consolidation tenant, K7-D-5)."""
    registry.register(
        JobTypeSpec(
            type=GRAPH_CONSOLIDATION_JOB_TYPE,
            payload_model=ConsolidationJobPayload,
            handler=ConsolidationHandler(pass_=pass_),
            idempotency_key=consolidation_idempotency_key,
            retry=RetryPolicy(max_attempts=2),
            lease=MEDIUM_LEASE,
        )
    )


def enqueue_graph_consolidation(
    queue: JobQueue,
    *,
    owner_id: str,
    delay_seconds: float,
    bucket_seconds: float,
    now: datetime | None = None,
) -> None:
    """Enqueue a coalesced consolidation run for ``owner_id`` (the synthesis-tail trigger).

    Called at the synthesis job's tail when the graph was dirtied. The
    ``watermark_bucket`` key coalesces a burst to one run; ``scheduled_at`` defers it
    to the idle boundary (K7-D-5). An identical re-enqueue within the bucket is A0's
    ``ON CONFLICT`` no-op. Off the critical path.
    """
    moment = now or datetime.now(UTC)
    bucket = str(int(moment.timestamp() // bucket_seconds)) if bucket_seconds > 0 else "0"
    payload = ConsolidationJobPayload(watermark_bucket=bucket)
    queue.enqueue(
        type=GRAPH_CONSOLIDATION_JOB_TYPE,
        owner_id=owner_id,
        payload=payload.model_dump(),
        idempotency_key=consolidation_idempotency_key(payload),
        scheduled_at=moment + timedelta(seconds=delay_seconds),
    )
