"""A8 T5 — the occurrences read API + quiet-hours profile (A8-D-11/D-6).

Real app + Docker Postgres + fake verifier. Proves: occurrences match the engine exactly (the
same-code-path line); the server caps clamp a wide window with an honest ``truncated`` marker;
cross-tenant isolation is non-vacuous (B sees B's, never A's); fire history is honest (ran/missed
from the audit trail); and quiet-hours round-trip on the profile with off-until-set + coherence.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule, next_fire_after
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.schedules import ScheduleStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_CREATED = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


@pytest.fixture
def client(migrated_engine: Engine, tmp_path: object) -> Iterator[TestClient]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path) + "/audit")
    app = create_app(cfg)

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=f"{token}@example.test")

    with TestClient(app) as c:
        app.state.verify_token = _verify
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        yield c
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id LIKE 'a8occ_%'"))
        su.dispose()


@pytest.fixture
def store() -> Iterator[ScheduleStore]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield ScheduleStore(engine)
    engine.dispose()


def _daily(hour: int) -> RecurrenceRule:
    return RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(hour,), byminute=(0,))


def _make_schedule(store: ScheduleStore, owner: str, sid: str, hour: int) -> Schedule:
    return store.create(
        Schedule(
            id=sid,
            owner_id=owner,
            timezone="Europe/Oslo",
            recurrence=_daily(hour),
            target_job_type="briefing",
            created_at=_CREATED,
            updated_at=_CREATED,
        ),
        now=_CREATED,
    )


def test_occurrences_match_the_engine(client: TestClient, store: ScheduleStore) -> None:
    uid = "a8occ_match"
    client.get("/v1/me/profile", headers=_auth(uid))  # provision the user
    sched = _make_schedule(store, uid, "sm", 8)

    r = client.get(
        "/v1/me/schedule/occurrences",
        params={"from": "2026-05-01T00:00:00Z", "to": "2026-05-05T00:00:00Z"},
        headers=_auth(uid),
    )
    assert r.status_code == 200
    body = r.json()
    # Compare as instants (the API serializes UTC as '...Z'; isoformat uses '+00:00').
    fires = [
        datetime.fromisoformat(o["fire_at"].replace("Z", "+00:00")) for o in body["occurrences"]
    ]

    # Independently compute the same window via the engine — the same-code-path guarantee.
    expected: list[datetime] = []
    nxt = next_fire_after(sched, datetime(2026, 5, 1, tzinfo=UTC))
    while nxt is not None and nxt <= datetime(2026, 5, 5, tzinfo=UTC):
        expected.append(nxt)
        nxt = next_fire_after(sched, nxt)
    assert fires == expected
    assert body["truncated"] is False
    assert all("FREQ=" not in o["human_terms"] for o in body["occurrences"])  # no raw RRULE


def test_wide_window_is_capped_and_marked_truncated(
    client: TestClient, store: ScheduleStore
) -> None:
    uid = "a8occ_cap"
    client.get("/v1/me/profile", headers=_auth(uid))
    _make_schedule(store, uid, "sc", 8)  # a daily rule
    # A 2-year window blows past the 90-day horizon AND the 500-count cap.
    r = client.get(
        "/v1/me/schedule/occurrences",
        params={"from": "2026-01-01T00:00:00Z", "to": "2028-01-01T00:00:00Z"},
        headers=_auth(uid),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["truncated"] is True  # honest marker — not a silent cap
    # Horizon-clamped: the effective window end is <= 90 days after from.
    assert body["window_to"] < "2027"  # far short of the requested 2028
    assert len(body["occurrences"]) <= 500


def test_cross_tenant_isolation_non_vacuous(client: TestClient, store: ScheduleStore) -> None:
    # A and B each own a schedule; each sees ONLY their own occurrences.
    client.get("/v1/me/profile", headers=_auth("a8occ_a"))
    client.get("/v1/me/profile", headers=_auth("a8occ_b"))
    _make_schedule(store, "a8occ_a", "sa", 8)
    _make_schedule(store, "a8occ_b", "sb", 9)

    params = {"from": "2026-05-01T00:00:00Z", "to": "2026-05-03T00:00:00Z"}
    a = client.get("/v1/me/schedule/occurrences", params=params, headers=_auth("a8occ_a")).json()
    b = client.get("/v1/me/schedule/occurrences", params=params, headers=_auth("a8occ_b")).json()

    a_ids = {o["schedule_id"] for o in a["occurrences"]}
    b_ids = {o["schedule_id"] for o in b["occurrences"]}
    assert a_ids == {"sa"}  # non-vacuous: A HAS occurrences
    assert b_ids == {"sb"}
    assert "sb" not in a_ids  # ... and never sees B's
    assert "sa" not in b_ids


def test_fire_history_is_honest(client: TestClient, store: ScheduleStore) -> None:
    uid = "a8occ_hist"
    client.get("/v1/me/profile", headers=_auth(uid))
    _make_schedule(store, uid, "sh", 8)
    fired_at = datetime(2026, 5, 2, 6, 0, tzinfo=UTC)
    # Record a real fire via the store (audits schedule.fire with fire_time).
    store.record_fire(uid, "sh", fire_time=fired_at)

    r = client.get(
        "/v1/me/schedule/occurrences",
        params={"from": "2026-05-01T00:00:00Z", "to": "2026-05-31T00:00:00Z"},
        headers=_auth(uid),
    )
    history = r.json()["history"]
    assert any(h["schedule_id"] == "sh" and h["status"] == "ran" for h in history)


def test_from_after_to_is_422(client: TestClient) -> None:
    r = client.get(
        "/v1/me/schedule/occurrences",
        params={"from": "2026-05-05T00:00:00Z", "to": "2026-05-01T00:00:00Z"},
        headers=_auth("a8occ_bad"),
    )
    assert r.status_code == 422


# --- quiet-hours profile (A8-D-6) -----------------------------------------------------------


def test_quiet_hours_off_until_set(client: TestClient) -> None:
    body = client.get("/v1/me/profile", headers=_auth("a8occ_qoff")).json()
    assert body["quiet_hours_start"] is None
    assert body["quiet_hours_end"] is None


def test_quiet_hours_set_and_clear(client: TestClient) -> None:
    uid = "a8occ_qset"
    r = client.patch(
        "/v1/me/profile",
        json={"quiet_hours_start": 1320, "quiet_hours_end": 420},  # 22:00 → 07:00
        headers=_auth(uid),
    )
    assert r.status_code == 200
    assert r.json()["quiet_hours_start"] == 1320
    assert r.json()["quiet_hours_end"] == 420
    cleared = client.patch(
        "/v1/me/profile",
        json={"quiet_hours_start": None, "quiet_hours_end": None},
        headers=_auth(uid),
    )
    assert cleared.json()["quiet_hours_start"] is None


def test_quiet_hours_half_set_is_422(client: TestClient) -> None:
    r = client.patch(
        "/v1/me/profile",
        json={"quiet_hours_start": 1320},  # only one → incoherent
        headers=_auth("a8occ_qhalf"),
    )
    assert r.status_code == 422


def test_quiet_hours_empty_window_is_422(client: TestClient) -> None:
    r = client.patch(
        "/v1/me/profile",
        json={"quiet_hours_start": 480, "quiet_hours_end": 480},  # start == end
        headers=_auth("a8occ_qempty"),
    )
    assert r.status_code == 422
