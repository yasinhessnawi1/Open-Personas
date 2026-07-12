"""R9-024 — the chat right-panel calendar's API leg: the ``?persona_id=`` filter + delete verb.

Real app + real Postgres + fake verifier (mirrors ``test_a8_occurrences.py``). Proves:

1. The persona filter matches BOTH linkage kinds — a task-backed schedule
   (``task_scheduled_fire``, resolved through the tasks join) and a schedule with no backing
   task (``initiative_scan``-style, resolved from its own ``payload_template.persona_id``).
2. Another persona's (and another owner's) schedule is excluded — non-vacuously (each side
   HAS occurrences of its own).
3. Omitting ``persona_id`` is byte-identical to pre-R9-024: every owned schedule's occurrences,
   unfiltered — the additive-optional contract.
4. The new ``DELETE /v1/me/schedule/{schedule_id}`` verb: removes the row (204, gone from a
   follow-up list), 404s on a missing id, and 404s (never deletes) cross-tenant.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.schedules import (
    RecurrenceFreq,
    RecurrenceKind,
    RecurrencePattern,
    RecurrenceRule,
    Schedule,
)
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.schedules import ScheduleStore
from persona_api.services.schedule_create_service import create_user_schedule
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_CREATED = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_WINDOW = {"from": "2026-05-01T00:00:00Z", "to": "2026-05-05T00:00:00Z"}


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
            conn.execute(text("DELETE FROM users WHERE id LIKE 'r9024_%'"))
        su.dispose()


@pytest.fixture
def engine() -> Iterator[Engine]:
    """The ``persona_app`` RLS engine the service runs on (seeding rides the superuser)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    eng = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine: Engine) -> ScheduleStore:
    return ScheduleStore(engine)


@pytest.fixture
def tasks(engine: Engine) -> TaskStore:
    return TaskStore(engine)


def _seed_owner_with_personas(seed: Engine, owner: str, persona_ids: list[str]) -> None:
    with seed.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": owner, "e": f"{owner}@example.com"},
        )
        for pid in persona_ids:
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
                {"p": pid, "o": owner},
            )


def _task_linked_schedule(
    engine: Engine, store: ScheduleStore, tasks: TaskStore, *, owner: str, persona: str, key: str
) -> str:
    """A ``task_scheduled_fire`` schedule (persona resolved via the tasks join)."""
    result = create_user_schedule(
        engine,
        store,
        tasks,
        owner_id=owner,
        pattern=RecurrencePattern(kind=RecurrenceKind.DAILY, hour=8, minute=0),
        one_time_at=None,
        timezone="Europe/Oslo",
        persona_id=persona,
        subject="task-linked reminder",
        idempotency_key=key,
        now=_NOW,
    )
    return result.schedule_id


def _payload_linked_schedule(
    store: ScheduleStore, *, owner: str, persona: str, schedule_id: str, hour: int = 9
) -> str:
    """An ``initiative_scan``-style schedule — persona lives directly in ``payload_template``,
    no backing ``tasks`` row (mirrors ``initiative.handler.ensure_initiative_schedule``)."""
    store.create(
        Schedule(
            id=schedule_id,
            owner_id=owner,
            timezone="Europe/Oslo",
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(hour,), byminute=(0,)),
            target_job_type="initiative_scan",
            payload_template={"persona_id": persona},
            created_at=_CREATED,
            updated_at=_CREATED,
        ),
        now=_CREATED,
    )
    return schedule_id


# --- the persona filter (both linkage kinds + exclusion + absent-param) --------------------


def test_persona_filter_matches_task_linked_schedule(
    client: TestClient,
    engine: Engine,
    store: ScheduleStore,
    tasks: TaskStore,
    migrated_engine: Engine,
) -> None:
    owner = "r9024_task"
    _seed_owner_with_personas(migrated_engine, owner, ["r9024_ta", "r9024_tb"])
    sched_a = _task_linked_schedule(
        engine, store, tasks, owner=owner, persona="r9024_ta", key="k-task-a"
    )

    r = client.get(
        "/v1/me/schedule/occurrences",
        params={**_WINDOW, "persona_id": "r9024_ta"},
        headers=_auth(owner),
    )
    assert r.status_code == 200
    ids = {o["schedule_id"] for o in r.json()["occurrences"]}
    assert ids == {sched_a}  # non-vacuous: the filter HAS a match


def test_persona_filter_matches_payload_linked_schedule(
    client: TestClient, engine: Engine, store: ScheduleStore, migrated_engine: Engine
) -> None:
    owner = "r9024_payload"
    _seed_owner_with_personas(migrated_engine, owner, ["r9024_pa"])
    sched_id = _payload_linked_schedule(
        store, owner=owner, persona="r9024_pa", schedule_id="r9024_initsched"
    )

    r = client.get(
        "/v1/me/schedule/occurrences",
        params={**_WINDOW, "persona_id": "r9024_pa"},
        headers=_auth(owner),
    )
    assert r.status_code == 200
    ids = {o["schedule_id"] for o in r.json()["occurrences"]}
    assert ids == {sched_id}  # non-vacuous: the payload-only linkage resolves too


def test_persona_filter_excludes_another_personas_schedule(
    client: TestClient,
    engine: Engine,
    store: ScheduleStore,
    tasks: TaskStore,
    migrated_engine: Engine,
) -> None:
    """One owner, two personas, both linkage kinds — filtering by A returns ONLY A's
    occurrences (both of A's schedules), never B's; and vice versa."""
    owner = "r9024_excl"
    _seed_owner_with_personas(migrated_engine, owner, ["r9024_ea", "r9024_eb"])
    sched_a_task = _task_linked_schedule(
        engine, store, tasks, owner=owner, persona="r9024_ea", key="k-excl-a"
    )
    sched_a_payload = _payload_linked_schedule(
        store, owner=owner, persona="r9024_ea", schedule_id="r9024_excl_a_init", hour=10
    )
    sched_b = _task_linked_schedule(
        engine, store, tasks, owner=owner, persona="r9024_eb", key="k-excl-b"
    )

    a = client.get(
        "/v1/me/schedule/occurrences",
        params={**_WINDOW, "persona_id": "r9024_ea"},
        headers=_auth(owner),
    ).json()
    b = client.get(
        "/v1/me/schedule/occurrences",
        params={**_WINDOW, "persona_id": "r9024_eb"},
        headers=_auth(owner),
    ).json()

    a_ids = {o["schedule_id"] for o in a["occurrences"]}
    b_ids = {o["schedule_id"] for o in b["occurrences"]}
    assert a_ids == {sched_a_task, sched_a_payload}
    assert b_ids == {sched_b}
    assert sched_b not in a_ids  # B's schedule never leaks into A's filtered view
    assert not (a_ids & b_ids)  # ... and disjoint the other direction too


def test_absent_persona_filter_is_unchanged(
    client: TestClient,
    engine: Engine,
    store: ScheduleStore,
    tasks: TaskStore,
    migrated_engine: Engine,
) -> None:
    """Omitting ``persona_id`` returns every owned schedule's occurrences — the
    additive-optional contract (today's behaviour, byte-identical)."""
    owner = "r9024_absent"
    _seed_owner_with_personas(migrated_engine, owner, ["r9024_aa", "r9024_ab"])
    sched_a = _task_linked_schedule(
        engine, store, tasks, owner=owner, persona="r9024_aa", key="k-absent-a"
    )
    sched_b = _payload_linked_schedule(
        store, owner=owner, persona="r9024_ab", schedule_id="r9024_absent_b_init"
    )

    r = client.get("/v1/me/schedule/occurrences", params=_WINDOW, headers=_auth(owner))
    assert r.status_code == 200
    ids = {o["schedule_id"] for o in r.json()["occurrences"]}
    assert ids == {sched_a, sched_b}  # the union — no filtering applied at all


# --- the delete verb -------------------------------------------------------------------


def test_delete_schedule_removes_it(
    client: TestClient,
    engine: Engine,
    store: ScheduleStore,
    tasks: TaskStore,
    migrated_engine: Engine,
) -> None:
    owner = "r9024_del"
    _seed_owner_with_personas(migrated_engine, owner, ["r9024_da"])
    sched_id = _task_linked_schedule(
        engine, store, tasks, owner=owner, persona="r9024_da", key="k-del"
    )

    r = client.delete(f"/v1/me/schedule/{sched_id}", headers=_auth(owner))
    assert r.status_code == 204

    follow_up = client.get(
        "/v1/me/schedule/occurrences", params=_WINDOW, headers=_auth(owner)
    ).json()
    assert sched_id not in {o["schedule_id"] for o in follow_up["occurrences"]}


def test_delete_missing_schedule_is_404(client: TestClient) -> None:
    r = client.delete("/v1/me/schedule/does-not-exist", headers=_auth("r9024_del_missing"))
    assert r.status_code == 404


def test_delete_is_cross_tenant_404_and_never_deletes(
    client: TestClient,
    engine: Engine,
    store: ScheduleStore,
    tasks: TaskStore,
    migrated_engine: Engine,
) -> None:
    owner_a = "r9024_del_a"
    owner_b = "r9024_del_b"
    _seed_owner_with_personas(migrated_engine, owner_a, ["r9024_daa"])
    _seed_owner_with_personas(migrated_engine, owner_b, ["r9024_dbb"])
    sched_id = _task_linked_schedule(
        engine, store, tasks, owner=owner_a, persona="r9024_daa", key="k-del-cross"
    )

    r = client.delete(f"/v1/me/schedule/{sched_id}", headers=_auth(owner_b))
    assert r.status_code == 404

    # A's row is untouched — still resolvable by its real owner.
    still_there = client.get(
        "/v1/me/schedule/occurrences", params=_WINDOW, headers=_auth(owner_a)
    ).json()
    assert sched_id in {o["schedule_id"] for o in still_there["occurrences"]}
