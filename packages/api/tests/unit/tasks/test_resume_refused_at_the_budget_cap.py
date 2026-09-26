"""Resume is refused on a task that has used its budget, and points to Extend (R9-158).

Before, Resume on a task paused at its cap cleared the pause and queued a leg boxed at
zero remaining budget: one model call, a salvage, another checkpoint counted toward the
leg cap, a cancelled run, and a new pause at the cap. Every click recreated the stale
"paused task, cancelled run" pair and spent money doing it. The owner ruled on 2026-09-26
that it is refused and points the user to Extend.

"At its cap" is read from the budget state (spent at or over the effective cap), not from
which door paused the task, so a task the user paused that is also at its cap is refused
too: resuming it would run the same zero-budget leg.

Driven through the real ``POST /v1/tasks/{id}/resume`` route on the community engine,
asserting the response, the durable row, and that no job was queued.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona.tasks import Contract, ContractBounds, CostLedger, Task, TaskState, WaitKind
from persona_api.approvals.budget import BudgetEnforcer, Extension, ExtensionOutcome
from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import personas as personas_t
from persona_api.jobs.queue import JobQueue
from persona_api.routes import tasks as tasks_route
from persona_api.routes.tasks import _extension_note
from persona_api.services import task_control_service
from persona_api.services.task_control_service import (
    BUDGET_REACHED_RESUME_NOTE,
    BUDGET_REACHED_WAITING_NOTE,
)
from persona_api.tasks.store import TaskStore
from sqlalchemy import func, insert, select

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
_OWNER = "user_budget_resume"
_PERSONA = "persona_budget_resume"
_CAP = 50_000  # five dollars, in ledger micros


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "budget_resume.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="b@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
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


def _task(
    engine: Engine,
    *,
    spent: int,
    state: TaskState = TaskState.ACTIVE,
    wait_kind: WaitKind | None = None,
) -> TaskStore:
    store = TaskStore(engine)
    store.create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(
                goal="find three sources", bounds=ContractBounds(total_budget_micros=_CAP)
            ),
            state=state,
            wait_kind=wait_kind,
            ledger=CostLedger(model_micros=spent),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    return store


def _jobs(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(conn.execute(select(func.count()).select_from(jobs_t)).scalar_one())


def _resume(client: TestClient) -> tuple[int, bool, str, bool]:
    res = client.post("/v1/tasks/t1/resume")
    body = res.json()
    return (res.status_code, body["changed"], body["note"], body["paused"])


def test_a_task_paused_by_its_budget_cap_is_refused_and_pointed_to_extend(
    engine: Engine, client: TestClient
) -> None:
    store = _task(engine, spent=_CAP)  # exactly at the cap: "at or over"
    budget = BudgetEnforcer(engine=engine, tasks=store, queue=JobQueue(engine))
    assert budget.enforce(_OWNER, store.get(_OWNER, "t1"), now=_NOW) is True  # the real pause
    assert _resume(client) == (200, False, BUDGET_REACHED_RESUME_NOTE, True)
    task = store.get(_OWNER, "t1")
    assert (task.state, task.paused, _jobs(engine)) == (TaskState.ACTIVE, True, 0)


def test_a_task_the_user_paused_that_is_over_its_cap_is_refused_too(
    engine: Engine, client: TestClient
) -> None:
    store = _task(engine, spent=_CAP + 1)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    assert _resume(client) == (200, False, BUDGET_REACHED_RESUME_NOTE, True)
    assert (store.get(_OWNER, "t1").paused, _jobs(engine)) == (True, 0)


def test_a_task_at_its_cap_waiting_on_the_user_is_not_picked_up_by_resume(
    engine: Engine, client: TestClient
) -> None:
    # Not paused, but Resume on a task waiting on the user becomes a pickup, which would
    # queue the same zero-budget leg.
    store = _task(engine, spent=_CAP, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    assert _resume(client) == (200, False, BUDGET_REACHED_WAITING_NOTE, False)
    task = store.get(_OWNER, "t1")
    assert (task.state, task.wait_kind, _jobs(engine)) == (TaskState.WAITING, WaitKind.ON_USER, 0)


def test_a_paused_task_under_its_cap_still_resumes(engine: Engine, client: TestClient) -> None:
    # One micro under the cap. Waiting on the clock, so Resume only clears the overlay (no
    # leg to queue, which A0's Postgres-only enqueue could not take here anyway).
    store = _task(engine, spent=_CAP - 1, state=TaskState.WAITING, wait_kind=WaitKind.UNTIL_TIME)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    assert _resume(client) == (200, True, "", False)


@pytest.mark.parametrize("note", [BUDGET_REACHED_RESUME_NOTE, BUDGET_REACHED_WAITING_NOTE])
def test_the_refusal_is_human_names_extend_and_carries_no_dashes(note: str) -> None:
    assert "budget" in note
    assert "Raise cap" in note  # the task page's Extend button, by its label
    assert "xtend" in note
    assert not any(chr(code) in note for code in (0x2013, 0x2014))  # en, em dash


# --- Pick up, the other door that runs a leg (T4b) ------------------------------------


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records the legs the pickup would queue (A0's enqueue is Postgres-only)."""
    keys: list[str] = []

    def _enqueue(self: JobQueue, **kwargs: object) -> None:  # noqa: ARG001
        trigger = dict(kwargs["payload"])["trigger"]  # type: ignore[call-overload]
        keys.append(f"{kwargs['idempotency_key']} {trigger['kind']}")

    monkeypatch.setattr(JobQueue, "enqueue", _enqueue)
    return keys


def _pickup(client: TestClient) -> tuple[int, bool, str]:
    res = client.post("/v1/tasks/t1/pickup")
    body = res.json()
    return (res.status_code, body["changed"], body["note"])


def test_pick_up_at_the_cap_is_refused_exactly_as_resume_is(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    store = _task(engine, spent=_CAP, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    assert _pickup(client) == (200, False, BUDGET_REACHED_WAITING_NOTE)
    task = store.get(_OWNER, "t1")
    assert (task.state, task.wait_kind, task.paused, enqueued, _jobs(engine)) == (
        TaskState.WAITING,
        WaitKind.ON_USER,
        False,
        [],
        0,
    )


def test_pick_up_under_the_cap_still_queues_the_next_leg(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    _task(engine, spent=_CAP - 1, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    assert (_pickup(client), enqueued) == ((200, True, ""), ["task:t1:after:init user_reply"])


# --- following the note: Raise cap, then carry on (R9-158 review, HIGH-1) ---------------


def _extend(client: TestClient, amount: int) -> tuple[int, bool, str, int]:
    res = client.post("/v1/tasks/t1/budget/extend", json={"amount_micros": amount})
    body = res.json()
    return (res.status_code, body["applied"], body["note"], body["new_cap_micros"])


def _extensions(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(audit_log_t)
                .where(audit_log_t.c.action == "budget.extended")
            ).scalar_one()
        )


def test_following_the_note_raise_cap_then_pick_up_queues_exactly_one_leg(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    # A leg crossed the cap and ended on a question: waiting on the user, not paused.
    store = _task(engine, spent=_CAP, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    assert _pickup(client) == (200, False, BUDGET_REACHED_WAITING_NOTE)
    # The note says: raise the cap, then answer it or pick it up. Raise cap:
    assert _extend(client, 10_000) == (200, True, "", _CAP + 10_000)
    task = store.get(_OWNER, "t1")
    assert (task.state, task.wait_kind, task.paused, enqueued) == (
        TaskState.WAITING,
        WaitKind.ON_USER,
        False,
        [],  # raising the cap queues nothing: it waits on the user, not on the budget
    )
    # ...then pick it up: exactly one leg.
    assert _pickup(client) == (200, True, "")
    assert enqueued == ["task:t1:after:init user_reply"]


def test_raise_cap_on_a_paused_task_waiting_on_a_question_runs_nothing_past_it(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    # A leg asked a question under a pause (T2 parks it WAITING, still paused), at the cap.
    store = _task(engine, spent=_CAP, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    assert _extend(client, 10_000) == (200, True, "", _CAP + 10_000)
    task = store.get(_OWNER, "t1")
    assert (task.state, task.wait_kind, task.paused, enqueued) == (
        TaskState.WAITING,
        WaitKind.ON_USER,
        False,  # the pause is cleared
        [],  # and no leg runs past the unanswered question
    )
    # The question moves on only with the answer.
    res = client.post("/v1/tasks/t1/reply", json={"reply": "Use the joint account."})
    assert (res.status_code, res.json()["changed"]) == (200, True)
    assert enqueued == ["task:t1:after:init user_reply"]


def test_a_double_raise_cap_extends_once(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    store = _task(engine, spent=_CAP)
    budget = BudgetEnforcer(engine=engine, tasks=store, queue=JobQueue(engine))
    assert budget.enforce(_OWNER, store.get(_OWNER, "t1"), now=_NOW) is True  # paused at cap
    first = _extend(client, 10_000)
    second = _extend(client, 10_000)  # the duplicated click
    assert (first, second) == (
        (200, True, "", _CAP + 10_000),
        (200, False, "This task isn't at its budget cap: nothing to extend.", _CAP + 10_000),
    )
    assert (_extensions(engine), store.get(_OWNER, "t1").paused, enqueued) == (
        1,
        False,
        ["task:t1:after:init revived"],  # the budget-paused ACTIVE task carries on once
    )


def test_raise_cap_by_too_little_says_how_much_is_needed_and_records_nothing(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    _task(engine, spent=_CAP + 5_000, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    assert _extend(client, 1_000) == (
        200,
        False,
        "That still leaves this task at its budget cap. Add more than $0.50 so it can carry on.",
        _CAP,
    )
    assert (_extensions(engine), enqueued) == (0, [])


def test_raise_cap_under_the_cap_or_on_a_finished_task_does_nothing(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    store = _task(engine, spent=_CAP - 1)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    assert _extend(client, 10_000)[1:3] == (
        False,
        "This task isn't at its budget cap: nothing to extend.",
    )
    assert store.get(_OWNER, "t1").paused is True  # a user's pause is not lifted by a no-op
    store.cancel(_OWNER, "t1", now=_NOW)
    assert _extend(client, 10_000)[1:3] == (
        False,
        "This task has already finished: nothing to extend.",
    )
    assert (_extensions(engine), enqueued) == (0, [])


@pytest.mark.parametrize("state", [TaskState.ACTIVE, TaskState.DEFINED])
def test_raise_cap_puts_the_leg_back_past_one_the_claim_already_consumed(
    engine: Engine, client: TestClient, enqueued: list[str], state: TaskState
) -> None:
    # A scheduled task paused at its cap: the schedule still fires, the worker claims the
    # leg, sees the pause and consumes it (the job succeeds without running). Raise cap must
    # not enqueue at that same key, where A0 would absorb it and leave nothing to run.
    store = _task(engine, spent=_CAP, state=state)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    with engine.begin() as conn:
        conn.execute(
            insert(jobs_t).values(
                id="job-consumed",
                type="task_leg",
                owner_id=_OWNER,
                payload={"task_id": "t1"},
                idempotency_key="task:t1:after:init",
                state="succeeded",
                attempt=1,
                max_attempts=5,
                scheduled_at=_NOW,
                created_at=_NOW,
            )
        )
    assert _extend(client, 10_000)[:3] == (200, True, "")
    task = store.get(_OWNER, "t1")
    assert (task.state, task.paused, enqueued) == (
        TaskState.ACTIVE,
        False,
        ["task:t1:after:init:retry:1 revived"],  # exactly one leg, keyed past the spent row
    )


# --- a task far over its cap can still be lifted off it (re-review LOW-3) ---------------


def _extend_raw(client: TestClient, amount: int) -> tuple[int, object]:
    res = client.post("/v1/tasks/t1/budget/extend", json={"amount_micros": amount})
    body = res.json()
    return (res.status_code, body.get("detail", body.get("note")))


def test_a_task_over_its_cap_by_more_than_one_request_can_still_be_lifted_off_it(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    # $15 over a $5 cap; one request may add $10, plus the $15 it is already over.
    store = _task(engine, spent=_CAP + 150_000, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    assert _extend_raw(client, 120_000) == (
        200,
        "That still leaves this task at its budget cap. Add more than $15.00 so it can carry on.",
    )
    assert _extend_raw(client, 250_001) == (
        422,
        "that is more than you can add at once, which is $25.00",
    )
    assert _extend(client, 250_000) == (200, True, "", _CAP + 250_000)
    task = store.get(_OWNER, "t1")
    assert (_extensions(engine), task.state, enqueued) == (1, TaskState.WAITING, [])


def test_at_the_cap_one_request_may_add_exactly_the_ceiling(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    _task(engine, spent=_CAP, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    assert _extend_raw(client, 100_001) == (
        422,
        "that is more than you can add at once, which is $10.00",
    )
    assert (_extend(client, 100_000), enqueued) == ((200, True, "", _CAP + 100_000), [])


@pytest.mark.parametrize(
    ("shortfall", "shown"),
    [(150_000, "$15.00"), (0, "$0.00"), (-10_000, "$0.00")],
)
def test_the_too_small_note_shows_the_decisions_own_shortfall_never_negative(
    shortfall: int, shown: str
) -> None:
    note = _extension_note(Extension(ExtensionOutcome.TOO_SMALL, shortfall))
    assert note == (
        f"That still leaves this task at its budget cap. Add more than {shown} so it can carry on."
    )


# --- the boundary: every recorded extension leaves the task BELOW its cap ----------------


def test_an_extension_of_exactly_the_shortfall_is_too_small_and_records_nothing(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    # $1.50 over the cap: adding exactly $1.50 would leave spent == cap, still at the cap.
    store = _task(engine, spent=_CAP + 15_000, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    assert _extend(client, 15_000) == (
        200,
        False,
        "That still leaves this task at its budget cap. Add more than $1.50 so it can carry on.",
        _CAP,
    )
    budget = BudgetEnforcer(engine=engine, tasks=store, queue=JobQueue(engine))
    task = store.get(_OWNER, "t1")
    assert (_extensions(engine), budget.check(_OWNER, task).value, enqueued) == (
        0,
        "reached",
        [],
    )


def test_the_shortfall_plus_one_micro_applies_and_leaves_the_task_below_its_cap(
    engine: Engine, client: TestClient, enqueued: list[str]
) -> None:
    store = _task(engine, spent=_CAP + 15_000, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    assert _extend(client, 15_001) == (200, True, "", _CAP + 15_001)
    budget = BudgetEnforcer(engine=engine, tasks=store, queue=JobQueue(engine))
    task = store.get(_OWNER, "t1")
    assert task.ledger.total_micros < budget.effective_cap(_OWNER, task)
    assert (_extensions(engine), budget.check(_OWNER, task).value, enqueued) == (
        1,
        "approaching",
        [],
    )


@pytest.mark.parametrize(("amount", "applied"), [(250_000, True), (250_001, False)])
def test_the_ceiling_boundary_is_the_ceiling_plus_the_shortfall(
    engine: Engine, client: TestClient, enqueued: list[str], amount: int, applied: bool
) -> None:
    _task(engine, spent=_CAP + 150_000, state=TaskState.WAITING, wait_kind=WaitKind.ON_USER)
    status, detail = _extend_raw(client, amount)
    assert (status, _extensions(engine), enqueued) == ((200, 1, []) if applied else (422, 0, []))
    assert detail == ("" if applied else "that is more than you can add at once, which is $25.00")
