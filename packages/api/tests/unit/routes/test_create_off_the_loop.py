"""A create, and its undo, never hold the event loop (R9-293 review HIGH-1).

The create embeds the identity synchronously, and a failed create's undo (the persona
erasure) may wait on the graph maintenance lock. Run on the loop, one slow create froze every
other request. The first request here blocks until the second has finished; on the loop, the
second could not start and the first would give up waiting.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.routes import personas as personas_routes

_OWNER = "u_loop"
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


def test_a_slow_create_does_not_hold_up_another_request(monkeypatch: pytest.MonkeyPatch) -> None:
    second_done = threading.Event()
    order: list[str] = []

    def _create(**kwargs: Any) -> str:  # noqa: ANN401
        if "slow" in kwargs["yaml_str"]:
            # The undo's stand-in: an erasure waiting on the graph lock.
            order.append("slow waited" if second_done.wait(5) else "slow gave up")
            return "persona_slow"
        order.append("quick")
        second_done.set()
        return "persona_quick"

    monkeypatch.setattr(personas_routes.persona_service, "create_persona", _create)
    monkeypatch.setattr(
        personas_routes.persona_service, "get_persona", lambda **k: _row(k["persona_id"])
    )
    monkeypatch.setattr(personas_routes.audit_service, "record", lambda **_: None)
    monkeypatch.setattr(personas_routes, "_tier_registry", lambda _r: None)
    monkeypatch.setattr(personas_routes, "_wants_portrait", lambda *_a, **_k: False)
    monkeypatch.setattr(
        personas_routes.notifications_service, "publish_sidebar_changed", lambda *_a, **_k: None
    )

    async def _no_enrichment(*_a: Any, **_k: Any) -> None:  # noqa: ANN401
        return None

    monkeypatch.setattr(personas_routes, "_enrich_persona_after_create", _no_enrichment)
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://u:p@localhost:1/none",  # never connected
            app_database_url="postgresql+psycopg://persona_app@localhost:1/none",
            edition=Edition.cloud,
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
    headers = {"Authorization": f"Bearer {_OWNER}"}

    async def _both() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            slow = asyncio.create_task(
                client.post("/v1/personas", json={"yaml": _YAML + "# slow\n"}, headers=headers)
            )
            await asyncio.sleep(0.2)
            quick = await client.post("/v1/personas", json={"yaml": _YAML}, headers=headers)
            return [quick.status_code, (await slow).status_code]

    codes = asyncio.run(_both())

    assert order == ["quick", "slow waited"]
    assert codes == [201, 201]
