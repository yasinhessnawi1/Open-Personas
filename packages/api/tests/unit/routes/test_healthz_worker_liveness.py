"""`/healthz` reports background-worker liveness (R9-093 observability half).

On 2026-08-01 the in-process worker loop died and every scheduled job stopped
for an hour while `/livez` returned 200 the whole time. Supervision (R9-093)
now restarts a CRASHED loop, but it cannot detect the other shape -- a loop
that is alive and wedged on a hung await, where nothing ever raises. A frozen
heartbeat catches that, and `/healthz` is where it surfaces.

Deliberately NOT on `/livez`: that is Fly's machine gate, so failing it would
kill a healthy HTTP surface over a background fault.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona_api.routes.health import router


class _FakeWorker:
    def __init__(self, last_beat_at: datetime | None) -> None:
        self.last_beat_at = last_beat_at


class _OkEngine:
    """Minimal engine stand-in whose `SELECT 1` succeeds."""

    def connect(self) -> _OkEngine:
        return self

    def __enter__(self) -> _OkEngine:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None


def _client(worker: object | None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.rls_engine = _OkEngine()
    app.state.in_process_worker = worker
    return TestClient(app)


def test_a_beating_worker_is_ok() -> None:
    """The healthy shape: a recent beat reports ok with its age."""
    with _client(_FakeWorker(datetime.now(UTC))) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["worker"] == "ok"
    assert body["worker_beat_age_seconds"] < 5


def test_a_stale_beat_degrades_healthz() -> None:
    """THE regression: a worker that stopped beating must stop reading healthy.

    This is the signal that was missing on 2026-08-01 -- an hour of no
    background work behind an entirely green health surface.
    """
    dead_since = datetime.now(UTC) - timedelta(minutes=30)
    with _client(_FakeWorker(dead_since)) as client:
        response = client.get("/healthz")
    assert response.status_code == 503, "a dead background loop must not report healthy"
    body = response.json()
    assert body["status"] == "degraded"
    assert body["worker"] == "stale"
    assert body["worker_beat_age_seconds"] > 60


def test_no_worker_configured_is_not_a_failure() -> None:
    """Most deployments run no in-process worker; that is configuration, not fault."""
    with _client(None) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["worker"] == "absent"
    assert "worker_beat_age_seconds" not in body


def test_a_worker_that_has_not_yet_beaten_is_starting_not_stale() -> None:
    """Between `start()` and the first iteration there is no beat yet.

    Reporting that as `stale` would make every boot flap 503.
    """
    with _client(_FakeWorker(None)) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["worker"] == "starting"


def test_livez_stays_green_regardless_of_the_worker() -> None:
    """`/livez` is the machine gate: a background fault must never fail it."""
    dead_since = datetime.now(UTC) - timedelta(hours=2)
    with _client(_FakeWorker(dead_since)) as client:
        response = client.get("/livez")
    assert response.status_code == 200, (
        "failing /livez on a background fault would kill a healthy serving process"
    )


@pytest.mark.parametrize("worker", [None, _FakeWorker(datetime.now(UTC))])
def test_db_failure_still_takes_precedence(worker: object) -> None:
    """A DB outage reports as such, whatever the worker is doing."""
    app = FastAPI()
    app.include_router(router)
    app.state.rls_engine = None
    app.state.in_process_worker = worker
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["db"] == "not_configured"
