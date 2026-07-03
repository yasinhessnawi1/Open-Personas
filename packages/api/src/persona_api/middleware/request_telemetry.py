"""Request-telemetry middleware — per-endpoint / status / p99, off the hot path.

Fills the §6.3 "system health" gap (R5-D-3): one ``request_telemetry`` row per
HTTP request carrying ``method`` · matched ``route_template`` (NOT the raw path —
cardinality) · ``status_code`` · ``duration_ms`` · ``timestamp``. Read by the
§6.3 Grafana dashboard through the ``grafana_ro BYPASSRLS`` role.

**The write NEVER sits on the request path (R5-D-3).** ``dispatch`` only appends a
small event to an in-process bounded ``deque`` — O(1), non-blocking, and
**drop-oldest** when full (``deque(maxlen=…)`` evicts the oldest on append, so a
saturated buffer never blocks a request and never grows unbounded). A background
task flushes the buffer to Postgres every few seconds via a batch INSERT run OFF
the event loop (``asyncio.to_thread`` — the engine is sync). The whole flush is
**fail-soft**: a DB error drops that batch and logs, it never propagates to a
request. A dropped telemetry row is invisible to users; a blocked request is not.

Chosen over the mainstream Prometheus-histogram middleware because that is
per-process — it cannot aggregate across N workers / N Fly Machines without a
Prometheus server (the "new metrics platform" the kickoff forbids) and bypasses
the existing ``turn_logs`` + ``grafana_ro`` + Grafana consumer (R5-D-3 rationale).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import TYPE_CHECKING

from persona.logging import get_logger
from sqlalchemy import insert
from starlette.middleware.base import BaseHTTPMiddleware

from persona_api.db.models import request_telemetry as request_telemetry_t

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy import Engine
    from starlette.requests import Request
    from starlette.responses import Response

_log = get_logger("api.telemetry")

__all__ = ["RequestTelemetryMiddleware", "TelemetryBuffer", "TelemetryEvent"]

# Defaults: a 10k-row buffer holds ~a request/sec for hours between flushes; a 5s
# flush interval keeps the dashboard near-live; 1k rows/batch bounds each INSERT.
_DEFAULT_CAPACITY = 10_000
_DEFAULT_FLUSH_INTERVAL_S = 5.0
_DEFAULT_BATCH = 1_000


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """One request's captured telemetry (built in the middleware, flushed later)."""

    timestamp: datetime
    method: str
    route_template: str
    status_code: int
    duration_ms: float


class TelemetryBuffer:
    """Bounded, drop-oldest in-process buffer + a periodic fail-soft batch flush.

    ``record`` is safe to call on the request path (a bare ``deque.append`` —
    non-blocking, cannot raise for our payloads, drops the oldest event when
    full). ``start`` launches the flush loop on the running event loop; ``aclose``
    cancels it and drains what remains, best-effort.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        capacity: int = _DEFAULT_CAPACITY,
        flush_interval_s: float = _DEFAULT_FLUSH_INTERVAL_S,
        batch_size: int = _DEFAULT_BATCH,
    ) -> None:
        self._engine = engine
        self._buf: deque[TelemetryEvent] = deque(maxlen=capacity)
        self._interval = flush_interval_s
        self._batch = batch_size
        self._task: asyncio.Task[None] | None = None

    def record(self, event: TelemetryEvent) -> None:
        """Append one event. Non-blocking, drop-oldest — NEVER blocks a request."""
        self._buf.append(event)

    def start(self) -> None:
        """Launch the periodic flush loop (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="request-telemetry-flush")

    async def aclose(self) -> None:
        """Stop the loop and drain the remaining buffer (best-effort)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._flush_once()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._flush_once()

    async def _flush_once(self) -> None:
        rows: list[dict[str, object]] = []
        while self._buf and len(rows) < self._batch:
            ev = self._buf.popleft()
            rows.append(
                {
                    "timestamp": ev.timestamp,
                    "method": ev.method,
                    "route_template": ev.route_template,
                    "status_code": ev.status_code,
                    "duration_ms": ev.duration_ms,
                }
            )
        if not rows:
            return
        try:
            # Sync engine → run the INSERT off the event loop so the flush never
            # stalls request handling.
            await asyncio.to_thread(self._write, rows)
        except Exception as exc:  # noqa: BLE001 — telemetry is best-effort; a dropped batch never breaks a request
            _log.warning(
                "telemetry flush dropped rows count={count} reason={reason}",
                count=len(rows),
                reason=str(exc)[:120],
            )

    def _write(self, rows: list[dict[str, object]]) -> None:
        with self._engine.begin() as conn:
            conn.execute(insert(request_telemetry_t), rows)


class RequestTelemetryMiddleware(BaseHTTPMiddleware):
    """Times each request and records it into ``app.state.telemetry_buffer``.

    No-ops when no buffer is present (telemetry disabled / no DB engine), so the
    only cost then is a monotonic-clock read. The DB write happens in the buffer's
    background flush, never here.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = perf_counter()
        response = await call_next(request)
        buffer: TelemetryBuffer | None = getattr(request.app.state, "telemetry_buffer", None)
        if buffer is not None:
            # The matched route's template (``/v1/personas/{persona_id}``), set in
            # the scope by Starlette routing. Missing on a 404 (no route matched) →
            # a fixed low-cardinality label, NEVER the raw path.
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            buffer.record(
                TelemetryEvent(
                    timestamp=datetime.now(UTC),
                    method=request.method,
                    route_template=template,
                    status_code=response.status_code,
                    duration_ms=(perf_counter() - start) * 1000.0,
                )
            )
        return response
