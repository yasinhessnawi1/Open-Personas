"""The task detail says whether the next leg is starting (R9-158).

Between a Resume and the worker claiming the leg there is no new run row, so the page
used to show a paused-looking task beside its old cancelled run. ``leg_queued`` is the
signal the page reads to show the next run as starting and to keep polling until the run
row appears: a leg claimed, or queued and due. A running leg has its own run row, and a
leg queued for later is not starting. The revival sweep asks the wider question over the
same base (running included); both are in ``tasks/live_legs.py``.

Driven through the real ``GET /v1/tasks/{id}`` route on the community engine, with real
``jobs`` rows.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona.tasks import Contract, Task, TaskState
from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.routes import tasks as tasks_route
from persona_api.tasks.live_legs import live_task_leg_ids, starting_task_leg_ids
from persona_api.tasks.store import TaskStore
from sqlalchemy import exists, insert, select

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
_OWNER = "user_leg_queued"
_PERSONA = "persona_leg_queued"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "leg_queued.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="q@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    TaskStore(eng).create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="find three sources"),
            state=TaskState.ACTIVE,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine: Engine) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(tasks_route.router)
    app.state.rls_engine = engine
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(id=_OWNER, email=None)
    with TestClient(app) as c:
        yield c


_DUE = datetime(2000, 1, 1, tzinfo=UTC)  # long past, whatever the clock says
_LATER = datetime(2999, 1, 1, tzinfo=UTC)  # a timed continuation, a retry backing off


def _job(
    engine: Engine,
    key: str,
    *,
    state: str,
    task_id: str = "t1",
    kind: str = "task_leg",
    scheduled_at: datetime = _DUE,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(jobs_t).values(
                id=f"job-{key}",
                type=kind,
                owner_id=_OWNER,
                payload={"task_id": task_id},
                idempotency_key=key,
                state=state,
                attempt=0,
                max_attempts=5,
                scheduled_at=scheduled_at,
                created_at=_NOW,
            )
        )


def _leg_queued(client: TestClient) -> bool:
    res = client.get("/v1/tasks/t1")
    assert res.status_code == 200, res.text
    return bool(res.json()["leg_queued"])


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("queued", True),
        ("claimed", True),
        ("running", True),  # no run row opened for it yet (see the run-row tests)
        ("succeeded", False),
        ("dead", False),
        ("failed", False),
    ],
)
def test_the_detail_reports_a_leg_that_is_starting(
    engine: Engine, client: TestClient, state: str, expected: bool
) -> None:
    _job(engine, "task:t1:after:0", state=state)
    assert _leg_queued(client) is expected


def _run_row(engine: Engine, run_id: str, *, started_at: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(runs_t).values(
                id=run_id,
                owner_id=_OWNER,
                persona_id=_PERSONA,
                task="find three sources",
                task_id="t1",
                status="running",
                started_at=started_at,
            )
        )


def test_a_running_leg_whose_run_row_has_opened_is_shown_as_the_run_not_as_starting(
    engine: Engine, client: TestClient
) -> None:
    _job(engine, "task:t1:after:0", state="running", scheduled_at=_DUE)
    _run_row(engine, "run-now", started_at=datetime(2000, 1, 1, 0, 0, 5, tzinfo=UTC))
    assert _leg_queued(client) is False


def test_a_running_leg_is_still_starting_while_only_an_older_run_exists(
    engine: Engine, client: TestClient
) -> None:
    # The previous leg's run started before this job was even scheduled: it is not this
    # leg's run, so the page keeps saying "Starting" (and keeps polling) until it opens.
    _job(engine, "task:t1:after:0", state="running", scheduled_at=_DUE)
    _run_row(engine, "run-before", started_at=datetime(1999, 12, 31, tzinfo=UTC))
    assert _leg_queued(client) is True


def test_a_leg_queued_for_later_is_not_starting(engine: Engine, client: TestClient) -> None:
    _job(engine, "task:t1:after:0", state="queued", scheduled_at=_LATER)
    assert _leg_queued(client) is False


@pytest.mark.parametrize(
    ("state", "scheduled_at", "live", "starting"),
    [
        ("queued", _DUE, True, True),
        ("queued", _LATER, True, False),
        ("claimed", _DUE, True, True),
        # Claimed is starting whatever the row's time says: a worker whose clock runs ahead
        # of this process may claim a leg this process would still call "later".
        ("claimed", _LATER, True, True),
        ("running", _DUE, True, True),
        ("succeeded", _DUE, False, False),
    ],
)
def test_the_sweep_and_the_detail_ask_two_questions_of_one_base(
    engine: Engine, state: str, scheduled_at: datetime, live: bool, starting: bool
) -> None:
    _job(engine, "task:t1:after:0", state=state, scheduled_at=scheduled_at)
    with engine.connect() as conn:
        answers = (
            bool(conn.execute(select(exists(live_task_leg_ids("t1")))).scalar_one()),
            bool(
                conn.execute(
                    select(exists(starting_task_leg_ids("t1", now=datetime.now(UTC))))
                ).scalar_one()
            ),
        )
    assert answers == (live, starting)


def test_no_job_at_all_is_no_leg_queued(client: TestClient) -> None:
    assert _leg_queued(client) is False


def test_another_tasks_leg_or_another_kind_of_job_is_not_this_tasks_leg(
    engine: Engine, client: TestClient
) -> None:
    _job(engine, "task:t2:after:0", state="queued", task_id="t2")
    _job(engine, "digest:t1", state="queued", kind="digest")
    assert _leg_queued(client) is False


def test_another_owners_leg_for_the_same_task_id_is_not_counted(
    engine: Engine, client: TestClient
) -> None:
    # The owner filter is explicit, not left to RLS (the community engine has none).
    ensure_owner(engine, owner_id="someone_else", email="e@example.com")
    with engine.begin() as conn:
        conn.execute(
            insert(jobs_t).values(
                id="job-foreign",
                type="task_leg",
                owner_id="someone_else",
                payload={"task_id": "t1"},
                idempotency_key="task:t1:after:0",
                state="queued",
                attempt=0,
                max_attempts=5,
                scheduled_at=_DUE,
                created_at=_NOW,
            )
        )
    assert _leg_queued(client) is False
