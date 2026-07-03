"""R5-D-3: the telemetry buffer is bounded, drop-oldest, and fail-soft.

These are the load-bearing properties that keep telemetry OFF the request path:
``record`` never blocks and silently drops the oldest event when full; a flush
that hits a dead DB drops the batch and returns — it never propagates.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona_api.middleware.request_telemetry import TelemetryBuffer, TelemetryEvent


class _StubConn:
    def __init__(self, sink: list[dict[str, object]]) -> None:
        self._sink = sink

    def __enter__(self) -> _StubConn:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, _stmt: object, rows: list[dict[str, object]]) -> None:
        self._sink.extend(rows)


class _StubEngine:
    """Captures executemany rows; optionally raises to exercise fail-soft."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.written: list[dict[str, object]] = []

    def begin(self) -> _StubConn:
        if self.fail:
            raise RuntimeError("db down")
        return _StubConn(self.written)


def _event(seq: int) -> TelemetryEvent:
    return TelemetryEvent(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        method="GET",
        route_template="/v1/personas/{persona_id}",
        status_code=200,
        duration_ms=float(seq),
    )


def test_record_drops_oldest_when_full() -> None:
    engine = _StubEngine()
    buf = TelemetryBuffer(engine, capacity=3)  # type: ignore[arg-type]
    for i in range(5):
        buf.record(_event(i))  # 0..4 — only the last 3 (2,3,4) survive
    # Drain synchronously via the private writer path.
    import asyncio

    asyncio.run(buf._flush_once())  # noqa: SLF001 — white-box drain for the test
    assert [r["duration_ms"] for r in engine.written] == [2.0, 3.0, 4.0]


@pytest.mark.asyncio
async def test_flush_is_failsoft_on_db_error() -> None:
    engine = _StubEngine(fail=True)
    buf = TelemetryBuffer(engine, capacity=100)  # type: ignore[arg-type]
    buf.record(_event(1))
    buf.record(_event(2))
    # A dead DB must NOT raise into the caller — the batch is dropped + logged.
    await buf._flush_once()  # noqa: SLF001 — white-box drain
    assert engine.written == []  # nothing persisted; no exception escaped


@pytest.mark.asyncio
async def test_flush_batches_and_empties_the_buffer() -> None:
    engine = _StubEngine()
    buf = TelemetryBuffer(engine, capacity=100, batch_size=10)  # type: ignore[arg-type]
    for i in range(7):
        buf.record(_event(i))
    await buf._flush_once()  # noqa: SLF001
    assert len(engine.written) == 7
    # A second flush finds nothing — the buffer was drained.
    await buf._flush_once()  # noqa: SLF001
    assert len(engine.written) == 7


@pytest.mark.asyncio
async def test_flush_respects_batch_size() -> None:
    engine = _StubEngine()
    buf = TelemetryBuffer(engine, capacity=100, batch_size=3)  # type: ignore[arg-type]
    for i in range(7):
        buf.record(_event(i))
    await buf._flush_once()  # noqa: SLF001 — one batch of 3
    assert len(engine.written) == 3
