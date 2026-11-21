"""A client that disconnects mid-create never skips the steps after it (R9-293 review LOW-6).

The create runs in a thread. When it returns, the route records the audit row, pings the
owner's other tabs and schedules the enrichment (voice and portrait). The worry was that a
client gone before that point cancels the handler under BaseHTTPMiddleware (the request
telemetry), leaving a complete persona with no audit row and no enrichment.

Checked on Starlette 1.1.0, FastAPI 0.136.3 and uvicorn 0.48.0: nothing cancels it. uvicorn
only marks the cycle disconnected and drops what is sent after that, and BaseHTTPMiddleware
never cancels the app task on a disconnect. This drives the real app's full middleware stack
over raw ASGI, as uvicorn does: the body arrives, then every further receive is a
disconnect, and every send is dropped. A middleware, or a Starlette, that cancelled on a
disconnect would fail it.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.middleware.request_telemetry import RequestTelemetryMiddleware
from persona_api.routes import personas as personas_routes

_OWNER = "u_gone"
_PID = "persona_gone_client"
_YAML = "schema_version: '1.0'\nidentity:\n  name: Astrid\n"


def _row(persona_id: str) -> dict[str, Any]:
    return {
        "id": persona_id,
        "yaml": _YAML,
        "schema_version": "1.0",
        "avatar_url": None,
        "consent_to_auto_dispatch": None,
        "consent_updated_at": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }


def _app(monkeypatch: pytest.MonkeyPatch, after: list[str]) -> Any:  # noqa: ANN401
    def _create(**_k: Any) -> str:  # noqa: ANN401
        time.sleep(0.3)  # the client is gone by the time this returns
        return _PID

    async def _enrich(*_a: Any, persona_id: str, **_k: Any) -> None:  # noqa: ANN401
        after.append(f"enrichment {persona_id}")

    monkeypatch.setattr(personas_routes.persona_service, "create_persona", _create)
    monkeypatch.setattr(
        personas_routes.persona_service, "get_persona", lambda **k: _row(k["persona_id"])
    )
    monkeypatch.setattr(
        personas_routes.audit_service,
        "record",
        lambda **k: after.append(f"audit {k['action']} {k['target']}"),
    )
    monkeypatch.setattr(
        personas_routes.notifications_service,
        "publish_sidebar_changed",
        lambda *_a, **k: after.append(f"ping {k['reason']}"),
    )
    monkeypatch.setattr(personas_routes, "_tier_registry", lambda _r: None)
    monkeypatch.setattr(personas_routes, "_wants_portrait", lambda *_a, **_k: False)
    monkeypatch.setattr(personas_routes, "_enrich_persona_after_create", _enrich)
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://u:p@localhost:1/none",  # never connected
            app_database_url="postgresql+psycopg://persona_app@localhost:1/none",
            edition=Edition.cloud,
            telemetry_enabled=True,  # the BaseHTTPMiddleware in question
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.rls_engine = object()
    app.state.embedder = object()
    app.state.audit_root = "/tmp/audit"
    app.state.job_queue = None
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return app


async def _post_then_vanish(app: Any) -> list[str]:  # noqa: ANN401
    body = json.dumps({"yaml": _YAML}).encode()
    first = True
    dropped: list[str] = []

    async def receive() -> dict[str, Any]:
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        dropped.append(message["type"])  # uvicorn drops a send to a disconnected cycle

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/personas",
        "raw_path": b"/v1/personas",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {_OWNER}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("203.0.113.9", 50000),
        "server": ("test", 80),
        "state": {},
    }
    await app(scope, receive, send)
    return dropped


def test_a_create_whose_client_left_still_records_pings_and_enriches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    after: list[str] = []
    app = _app(monkeypatch, after)
    assert RequestTelemetryMiddleware in {m.cls for m in app.user_middleware}

    dropped = asyncio.run(asyncio.wait_for(_post_then_vanish(app), timeout=10))

    assert after == [
        f"audit persona.create {_PID}",
        "ping persona.created",
        f"enrichment {_PID}",
    ]
    assert dropped[0] == "http.response.start"
