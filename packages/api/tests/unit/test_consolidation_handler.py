"""Unit tests for the Spec K7 T5 consolidation job handler + scope guard.

Fast, no-DB: the enqueue payload/key shape, the handler running the pass + metering
the summary, and the standing scope guard — NO voice-side graph/consolidation wiring
(voice graph retrieval stays unwired pending K4-gate routing, memory
``project_voice_graph_unwired``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from persona.graph.consolidation import ConsolidationReport
from persona_api.jobs.handlers.consolidation import (
    GRAPH_CONSOLIDATION_JOB_TYPE,
    ConsolidationHandler,
    ConsolidationJobPayload,
    consolidation_idempotency_key,
    enqueue_graph_consolidation,
)

if TYPE_CHECKING:
    from collections.abc import Mapping


# ----- enqueue payload + idempotency key -----------------------------------


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def test_enqueue_coalesces_by_bucket_and_defers() -> None:
    q = _RecordingQueue()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    enqueue_graph_consolidation(
        q,  # type: ignore[arg-type]
        owner_id="u1",
        delay_seconds=300.0,
        bucket_seconds=300.0,
        now=now,
    )
    call = q.calls[0]
    assert call["type"] == GRAPH_CONSOLIDATION_JOB_TYPE
    assert call["owner_id"] == "u1"
    # key coalesces to the 300s bucket; scheduled_at defers by the delay.
    bucket = str(int(now.timestamp() // 300.0))
    assert call["idempotency_key"] == f"graph_consolidation:{bucket}"
    assert call["scheduled_at"] == datetime(2026, 1, 1, 12, 5, tzinfo=UTC)


def test_idempotency_key_shape() -> None:
    assert (
        consolidation_idempotency_key(ConsolidationJobPayload(watermark_bucket="42"))
        == "graph_consolidation:42"
    )


# ----- handler runs the pass + meters --------------------------------------


class _FakePass:
    def __init__(self) -> None:
        self.ran_for: list[str] = []

    def run(self, owner_id: str) -> ConsolidationReport:
        self.ran_for.append(owner_id)
        return ConsolidationReport(
            run_id="u1:1",
            owner_id=owner_id,
            epoch=1,
            candidates_considered=3,
            clusters_formed=1,
            nodes_merged=2,
        )


class _FakeContext:
    owner_id = "u1"
    job_id = "job-1"

    def __init__(self) -> None:
        self.metered: list[dict[str, object]] = []

    def meter(
        self,
        *,
        amount_micros: int,  # noqa: ARG002 — protocol arg, unused by the fake
        kind: str,
        detail: Mapping[str, str] | None = None,
    ) -> None:
        self.metered.append({"kind": kind, "detail": dict(detail or {})})


def test_handler_runs_pass_and_meters_summary() -> None:
    pass_ = _FakePass()
    ctx = _FakeContext()
    handler = ConsolidationHandler(pass_=pass_)
    asyncio.run(handler.handle(ConsolidationJobPayload(watermark_bucket="0"), ctx))  # type: ignore[arg-type]
    assert pass_.ran_for == ["u1"]  # ran for the job owner
    assert ctx.metered[0]["detail"]["merged"] == "2"
    assert ctx.metered[0]["detail"]["surface"] == "graph_consolidation"


# ----- scope guard: no voice-side consolidation/graph wiring (bar 5) --------


def test_voice_has_no_consolidation_or_graph_wiring() -> None:
    voice_src = Path(__file__).resolve().parents[3] / "voice" / "src"
    assert voice_src.is_dir(), voice_src
    offenders = [
        str(f)
        for f in voice_src.rglob("*.py")
        if "ConsolidationPass" in (t := f.read_text()) or "graph_consolidation" in t
    ]
    assert offenders == [], f"voice must not wire graph consolidation (unwired rule): {offenders}"
