"""Health endpoints (spec 08, T12, §8.3).

``GET /healthz`` → 200 ``{"status":"ok","db":"connected","worker":…}`` when
Postgres is reachable AND the background worker is beating; 503 when either
fails. No auth (uptime monitors + load balancers hit it).

``GET /livez`` → always 200 ``{"status":"ok"}``. Liveness only — no dependency
checks. Fly.io's machine health check points here so a DB blip can't kill the
process; the deep ``/healthz`` readiness check stays for monitoring.

**Why worker liveness is on /healthz and NOT /livez (R9-093).** On 2026-08-01
the background loop died silently and every scheduled job stopped for an hour
while ``/livez`` returned 200 throughout — a false green that hid the outage
until a user noticed their schedule had never run. The fix is to make it
OBSERVABLE, not to make it fatal: ``/livez`` is Fly's machine gate, so failing
it would kill a perfectly healthy HTTP surface over a background fault, and
R9-093's supervision already restarts a crashed loop by itself. ``/healthz`` is
the monitoring surface, so a stale beat degrades THERE, where it is visible
without taking the API down.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

router = APIRouter(tags=["health"])

__all__ = ["router"]

#: How stale the worker's beat may get before ``/healthz`` reports it degraded.
#: The loop turns on a sub-second cadence when idle, so anything approaching a
#: minute means it is wedged or gone. Generous enough that a slow sweep (the
#: catalog sync clones a repo) never trips a false alarm.
_WORKER_BEAT_STALE_AFTER_SECONDS = 120.0


def _worker_health(request: Request) -> tuple[str, float | None]:
    """Classify the background worker: ``absent`` / ``starting`` / ``ok`` / ``stale``.

    ``absent`` is NOT a failure: most deployments (and every community/self-host
    boot) run no in-process worker at all, and an API serving without one is
    working as configured. Only a worker that EXISTS and has stopped beating is
    a fault.
    """
    worker = getattr(request.app.state, "in_process_worker", None)
    if worker is None:
        return "absent", None
    beat = worker.last_beat_at
    if beat is None:
        # Started but the loop has not completed its first iteration yet.
        return "starting", None
    age = (datetime.now(UTC) - beat).total_seconds()
    return ("stale" if age > _WORKER_BEAT_STALE_AFTER_SECONDS else "ok"), age


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Readiness: DB connectivity + background-worker liveness."""
    engine = getattr(request.app.state, "rls_engine", None)
    if engine is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "db": "not_configured"},
        )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 — any connectivity failure → 503
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "db": "disconnected"},
        )
    worker_state, beat_age = _worker_health(request)
    body: dict[str, object] = {"status": "ok", "db": "connected", "worker": worker_state}
    if beat_age is not None:
        body["worker_beat_age_seconds"] = round(beat_age, 1)
    if worker_state == "stale":
        body["status"] = "degraded"
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body)
    return JSONResponse(status_code=status.HTTP_200_OK, content=body)


@router.get("/livez")
async def livez() -> JSONResponse:
    """Liveness probe — process is up; no dependency checks."""
    return JSONResponse(status_code=status.HTTP_200_OK, content={"status": "ok"})
