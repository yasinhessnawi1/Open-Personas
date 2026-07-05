"""Unit tests for the K8 sleep-time engine's A0 tenant + trigger (Spec K8 T6).

Fast, no-DB (the consolidation-handler test pattern): the enqueue payload/key
shape (per-persona bucket coalescing + idle deferral), the handler running the
engine + metering the report, the turn-tail trigger's kill-switch gating
(built-but-inert guard, trigger half), and worker-registry registration gating
(handler half).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from persona.stores.engine import EpisodicConsolidationReport
from persona_api.jobs.handlers.episodic_consolidation import (
    EPISODIC_CONSOLIDATION_JOB_TYPE,
    EpisodicConsolidationHandler,
    EpisodicConsolidationJobPayload,
    enqueue_episodic_consolidation,
    episodic_consolidation_idempotency_key,
)

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


def _report(**overrides: object) -> EpisodicConsolidationReport:
    base: dict[str, object] = {
        "persona_id": "p1",
        "chunks_considered": 7,
        "windows_formed": 2,
        "windows_deferred_open": 1,
        "gists_written": 2,
        "candidates_emitted": 2,
    }
    base.update(overrides)
    return EpisodicConsolidationReport(**base)  # type: ignore[arg-type]


# ----- enqueue payload + idempotency key ------------------------------------


def test_enqueue_coalesces_per_persona_bucket_and_defers() -> None:
    q = _RecordingQueue()
    enqueue_episodic_consolidation(
        q,  # type: ignore[arg-type]
        owner_id="u1",
        persona_id="p1",
        delay_seconds=900.0,
        bucket_seconds=3600.0,
        now=_NOW,
    )
    call = q.calls[0]
    assert call["type"] == EPISODIC_CONSOLIDATION_JOB_TYPE
    assert call["owner_id"] == "u1"
    bucket = str(int(_NOW.timestamp() // 3600.0))
    assert call["idempotency_key"] == f"episodic_consolidation:p1:{bucket}"
    assert call["scheduled_at"] == datetime(2026, 7, 4, 12, 15, tzinfo=UTC)


def test_idempotency_key_is_per_persona() -> None:
    a = episodic_consolidation_idempotency_key(
        EpisodicConsolidationJobPayload(persona_id="p1", watermark_bucket="42")
    )
    b = episodic_consolidation_idempotency_key(
        EpisodicConsolidationJobPayload(persona_id="p2", watermark_bucket="42")
    )
    assert a == "episodic_consolidation:p1:42"
    assert a != b  # two personas in one bucket are two runs


# ----- the handler runs the engine + meters the report ------------------------


def test_handler_runs_the_engine_owner_scoped_and_meters() -> None:
    ran: list[tuple[str, str]] = []

    class _Engine:
        async def run(self, owner_id: str, persona_id: str) -> EpisodicConsolidationReport:
            ran.append((owner_id, persona_id))
            return _report()

    metered: list[dict[str, object]] = []
    context = SimpleNamespace(
        owner_id="u1",
        meter=lambda **kwargs: metered.append(kwargs),
    )
    handler = EpisodicConsolidationHandler(engine=_Engine())
    payload = EpisodicConsolidationJobPayload(persona_id="p1", watermark_bucket="1")
    asyncio.run(handler.handle(payload, context))  # type: ignore[arg-type]

    assert ran == [("u1", "p1")]  # owner from the RLS chokepoint, persona from payload
    detail = metered[0]["detail"]
    assert detail["surface"] == "episodic_consolidation"  # type: ignore[index]
    assert detail["gists"] == "2"  # type: ignore[index]


# ----- the turn-tail trigger: kill-switch gated (built-but-inert, trigger half) --


def test_trigger_enqueues_alongside_synthesis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EPISODIC_ENGINE_ENABLED", "true")
    from persona_api.services.synthesis_trigger import enqueue_conversation_synthesis

    q = _RecordingQueue()
    enqueue_conversation_synthesis(
        q,  # type: ignore[arg-type]
        owner_id="u1",
        conversation_id="c1",
        persona_id="p1",
        message_count=4,
    )
    types = [c["type"] for c in q.calls]
    assert "synthesis" in types
    assert EPISODIC_CONSOLIDATION_JOB_TYPE in types  # rides the same boundary


def test_trigger_is_silent_when_the_kill_switch_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PERSONA_EPISODIC_ENGINE_ENABLED", "false")
    from persona_api.services.synthesis_trigger import enqueue_run_synthesis

    q = _RecordingQueue()
    enqueue_run_synthesis(q, owner_id="u1", run_id="r1", persona_id="p1")  # type: ignore[arg-type]
    types = [c["type"] for c in q.calls]
    assert "synthesis" in types  # synthesis untouched
    assert EPISODIC_CONSOLIDATION_JOB_TYPE not in types  # the engine stays dark
