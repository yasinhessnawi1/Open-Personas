"""R5-D-3: the middleware captures the route TEMPLATE (not the raw path) + status.

The load-bearing cardinality property: two requests to ``/items/1`` and
``/items/2`` must record the SAME ``route_template`` (``/items/{item_id}``), not
two distinct raw paths — otherwise the per-endpoint dashboard explodes. Also
proves an unmatched (404) request is labelled ``unmatched``, never the raw path,
and that the write stays off the request path (recorded, flushed separately).
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona_api.middleware.request_telemetry import RequestTelemetryMiddleware, TelemetryBuffer


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
    def __init__(self) -> None:
        self.written: list[dict[str, object]] = []

    def begin(self) -> _StubConn:
        return _StubConn(self.written)


def _make_app(buffer: TelemetryBuffer) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestTelemetryMiddleware)

    @app.get("/items/{item_id}")
    def _get_item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    app.state.telemetry_buffer = buffer
    return app


def test_captures_route_template_not_raw_path() -> None:
    engine = _StubEngine()
    buffer = TelemetryBuffer(engine, capacity=100)  # type: ignore[arg-type]
    app = _make_app(buffer)
    with TestClient(app) as c:
        assert c.get("/items/1").status_code == 200
        assert c.get("/items/2").status_code == 200
    asyncio.run(buffer._flush_once())  # noqa: SLF001 — white-box drain

    assert len(engine.written) == 2
    # BOTH requests collapse to the same template — bounded cardinality.
    assert {r["route_template"] for r in engine.written} == {"/items/{item_id}"}
    row = engine.written[0]
    assert row["method"] == "GET"
    assert row["status_code"] == 200
    assert isinstance(row["duration_ms"], float)
    assert row["duration_ms"] >= 0.0


def test_unmatched_route_is_labelled_not_raw_path() -> None:
    engine = _StubEngine()
    buffer = TelemetryBuffer(engine, capacity=100)  # type: ignore[arg-type]
    app = _make_app(buffer)
    with TestClient(app) as c:
        assert c.get("/definitely/not/a/route").status_code == 404
    asyncio.run(buffer._flush_once())  # noqa: SLF001

    assert len(engine.written) == 1
    assert engine.written[0]["route_template"] == "unmatched"
    assert engine.written[0]["status_code"] == 404


def test_no_buffer_is_a_silent_noop() -> None:
    """Telemetry disabled / no engine ⇒ the middleware must not error."""
    app = FastAPI()
    app.add_middleware(RequestTelemetryMiddleware)

    @app.get("/ping")
    def _ping() -> dict[str, str]:
        return {"ok": "1"}

    app.state.telemetry_buffer = None
    with TestClient(app) as c:
        assert c.get("/ping").status_code == 200
