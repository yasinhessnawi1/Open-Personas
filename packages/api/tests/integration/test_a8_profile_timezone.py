"""Integration: the A8 per-user timezone on ``GET`` / ``PATCH /v1/me/profile`` (A8-D-9).

Drives the real app + Docker Postgres (migrated to head → migration 030's
``users.timezone`` exists) with a fake verifier. Asserts: a fresh account has a null
timezone (→ config-default fallback); a PATCH sets/clears it; an unknown IANA zone is
a fail-fast 422; setting the timezone leaves the name untouched; and — the load-bearing
D-9 property — changing the profile timezone does NOT silently re-anchor an existing
schedule's captured zone or its next fire (no silent re-anchor; D-A1-4 stability).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.schedules import RecurrenceFreq, RecurrenceRule, Schedule
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.schedules import ScheduleStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema (incl. migration 030) is at head
    tmp_path: object,
) -> Iterator[TestClient]:
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
            # schedules.owner_id FK → users ON DELETE CASCADE, so this clears both.
            conn.execute(text("DELETE FROM users WHERE id LIKE 'a8tz_%'"))
        su.dispose()


@pytest.fixture
def store() -> Iterator[ScheduleStore]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield ScheduleStore(engine)
    engine.dispose()


def test_fresh_account_has_null_timezone(client: TestClient) -> None:
    r = client.get("/v1/me/profile", headers=_auth("a8tz_fresh"))
    assert r.status_code == 200
    assert r.json()["timezone"] is None  # → schedule compute falls back to the config default


def test_patch_sets_timezone_then_get_reflects(client: TestClient) -> None:
    uid = "a8tz_set"
    r = client.patch("/v1/me/profile", json={"timezone": "Europe/Oslo"}, headers=_auth(uid))
    assert r.status_code == 200
    assert r.json()["timezone"] == "Europe/Oslo"
    assert client.get("/v1/me/profile", headers=_auth(uid)).json()["timezone"] == "Europe/Oslo"


def test_patch_null_clears_timezone(client: TestClient) -> None:
    uid = "a8tz_clear"
    client.patch("/v1/me/profile", json={"timezone": "America/New_York"}, headers=_auth(uid))
    r = client.patch("/v1/me/profile", json={"timezone": None}, headers=_auth(uid))
    assert r.status_code == 200
    assert r.json()["timezone"] is None


def test_unknown_timezone_rejected_422(client: TestClient) -> None:
    r = client.patch("/v1/me/profile", json={"timezone": "Mars/Phobos"}, headers=_auth("a8tz_bad"))
    assert r.status_code == 422


def test_setting_timezone_leaves_name_untouched(client: TestClient) -> None:
    uid = "a8tz_partial"
    client.patch("/v1/me/profile", json={"first_name": "Kari"}, headers=_auth(uid))
    r = client.patch("/v1/me/profile", json={"timezone": "Europe/Oslo"}, headers=_auth(uid))
    assert r.status_code == 200
    assert r.json()["first_name"] == "Kari"  # untouched by the tz PATCH
    assert r.json()["timezone"] == "Europe/Oslo"


def test_profile_timezone_change_does_not_re_anchor_existing_schedule(
    client: TestClient, store: ScheduleStore
) -> None:
    """D-9 (the load-bearing no-silent-re-anchor property).

    An existing schedule keeps its CAPTURED zone + next fire when the user changes
    their profile timezone — the profile PATCH touches only ``users``, never any
    schedule. Moving a live schedule's firing zone is an explicit reschedule (T6),
    never a side effect of a profile edit (D-A1-4 stability / the A5-class breach).
    """
    uid = "a8tz_reanchor"
    # Provision the user (ensure_user runs on the request) so the schedule FK resolves.
    client.get("/v1/me/profile", headers=_auth(uid))
    captured = Schedule(
        id="a8tz_sched",
        owner_id=uid,
        timezone="America/New_York",  # the schedule's captured zone
        recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,), byminute=(0,)),
        target_job_type="briefing",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    created = store.create(captured, now=datetime(2026, 1, 1, tzinfo=UTC))
    before_tz = created.timezone
    before_next_fire = created.next_fire_at

    # The user changes their PROFILE timezone to a different zone.
    r = client.patch("/v1/me/profile", json={"timezone": "Europe/Oslo"}, headers=_auth(uid))
    assert r.status_code == 200
    assert r.json()["timezone"] == "Europe/Oslo"

    # The existing schedule is UNTOUCHED: same captured zone, same next fire.
    after = store.get(uid, "a8tz_sched")
    assert after.timezone == before_tz == "America/New_York"
    assert after.next_fire_at == before_next_fire


def test_resolver_reads_stored_tz_from_db(client: TestClient) -> None:
    """The origination provider's DB-read composition: a set profile tz resolves to it.

    Exercises the exact two steps the ``_build_user_timezone_provider`` closure runs
    (``get_user_profile`` → ``resolve_timezone``) against the real DB.
    """
    from persona.timezone import resolve_timezone
    from persona_api.services import user_service

    uid = "a8tz_resolve_set"
    client.patch("/v1/me/profile", json={"timezone": "Australia/Sydney"}, headers=_auth(uid))
    engine = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        profile = user_service.get_user_profile(engine, user_id=uid)
        assert profile is not None
        resolved = resolve_timezone(profile.get("timezone"), default="Europe/Oslo")  # type: ignore[arg-type]
        assert resolved == "Australia/Sydney"
    finally:
        engine.dispose()


def test_resolver_falls_back_to_config_default_when_tz_unset(client: TestClient) -> None:
    """Unset ``users.timezone`` → the provider resolves to ``PERSONA_DEFAULT_TIMEZONE``."""
    from persona.timezone import resolve_timezone
    from persona_api.services import user_service

    uid = "a8tz_resolve_unset"
    client.get("/v1/me/profile", headers=_auth(uid))  # provision, no tz set
    engine = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        profile = user_service.get_user_profile(engine, user_id=uid)
        assert profile is not None
        assert profile.get("timezone") is None
        resolved = resolve_timezone(profile.get("timezone"), default="Europe/Oslo")  # type: ignore[arg-type]
        assert resolved == "Europe/Oslo"
    finally:
        engine.dispose()
