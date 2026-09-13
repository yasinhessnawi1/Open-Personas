"""The persona picks up its own stalled work, for real (Spec W1, T9; D-W1-10).

`task_introspect` let a persona SEE that a task had stalled and nothing let it act, so the
honest answer to "what happened to the research?" was a description of a dead end. This drives
the other half through the REAL chain: the tool the model would call, built by the REAL runtime
factory, resolving the REAL owner from the RLS contextvar, through the REAL control seam, to a
job the REAL worker claims and runs.

What is pinned: the tool call moves actual work; the persona cannot reach another tenant's task
or even another of its owner's personas' tasks, and a refusal looks identical to a made-up id;
the pause gates hold against the persona exactly as against the user; and every pickup leaves
an audit row that says the machine did it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.tasks import Contract, Task, TaskKind, TaskState, WaitKind
from persona_api.approvals.kill_switch import KillSwitchStore
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import task_control_service
from persona_api.tasks.store import TaskStore
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_UID = "user_w1_tool"
_OTHER_UID = "user_w1_tool_other"
_PERSONA = "persona_w1_tool"
_OTHER_PERSONA = "persona_w1_tool_second"
_NOW = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)


@pytest.fixture
def su_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ordering
    engine = make_rls_engine(os.environ["DATABASE_URL"])
    with engine.begin() as conn:
        for uid in (_UID, _OTHER_UID):
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": uid, "e": f"{uid}@x"},
            )
        for pid, owner in (
            (_PERSONA, _UID),
            (_OTHER_PERSONA, _UID),
            ("persona_theirs", _OTHER_UID),
        ):
            conn.execute(
                text(
                    "INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"i": pid, "o": owner, "y": "name: Astrid"},
            )
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": _UID, "b": _OTHER_UID})
    engine.dispose()


@pytest.fixture
def app_engine(su_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ordering
    engine = make_rls_engine(os.environ["APP_DATABASE_URL"].replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _factory(app_engine: Engine, embedder: object) -> object:
    from persona_api.services.runtime_factory import RuntimeFactory

    return RuntimeFactory(
        rls_engine=app_engine,
        embedder=embedder,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        audit_root=None,  # type: ignore[arg-type]
    )


def _pickup_tool(app_engine: Engine, persona_id: str = _PERSONA):  # noqa: ANN202
    """The tool as the factory builds it, with the port it binds to this persona."""
    from persona.tools.builtin.task_pickup import make_task_pickup_tool
    from persona_api.services.runtime_factory import RuntimeFactory

    factory = RuntimeFactory.__new__(RuntimeFactory)  # the provider needs only the engine
    factory._engine = app_engine  # noqa: SLF001
    return make_task_pickup_tool(
        port_provider=factory._build_task_pickup_provider(persona_id),  # noqa: SLF001
        persona_id=persona_id,
    )


def _parked_task(
    app_engine: Engine, task_id: str, *, owner: str = _UID, persona: str = _PERSONA
) -> Task:
    """A task waiting on the user, the shape a persona would find stalled."""
    store = TaskStore(app_engine)
    token = current_user_id.set(owner)
    try:
        store.create(
            Task(
                id=task_id,
                owner_id=owner,
                persona_id=persona,
                contract=Contract(goal="research the GitHub migration"),
                kind=TaskKind.AD_HOC,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        store.start(owner, task_id, now=datetime.now(UTC))
        store.begin_wait(owner, task_id, WaitKind.ON_USER, now=datetime.now(UTC))
        return store.get(owner, task_id)
    finally:
        current_user_id.reset(token)


def _jobs(su_engine: Engine, task_id: str) -> list[str]:
    with su_engine.begin() as conn:
        return [
            str(r[0])
            for r in conn.execute(
                text(
                    "SELECT state FROM jobs WHERE type = 'task_leg' "
                    "AND payload->>'task_id' = :t ORDER BY created_at"
                ),
                {"t": task_id},
            )
        ]


def _audit_rows(su_engine: Engine, task_id: str) -> list[dict[str, object]]:
    with su_engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT user_id, action, target, metadata FROM audit_log "
                    "WHERE target = :t AND action = :a"
                ),
                {"t": task_id, "a": task_control_service.PICKUP_AUDIT_ACTION},
            ).mappings()
        ]


@pytest.mark.asyncio
async def test_the_tool_call_moves_real_work(app_engine: Engine, su_engine: Engine) -> None:
    """The whole point: a model calling this makes a job the worker can claim."""
    task = _parked_task(app_engine, "t_tool_pickup")
    token = current_user_id.set(_UID)  # the request scope the tool resolves from
    try:
        result = await _pickup_tool(app_engine).execute(task_id=task.id)
    finally:
        current_user_id.reset(token)

    assert result.is_error is False
    assert result.data == {"changed": True, "note": "", "goal": "research the GitHub migration"}
    assert _jobs(su_engine, task.id) == ["queued"]  # a real leg, waiting for a real worker


@pytest.mark.asyncio
async def test_the_pickup_leaves_a_row_saying_the_machine_did_it(
    app_engine: Engine, su_engine: Engine
) -> None:
    """A persona acting on its own work must be legible afterwards: the audit says who."""
    task = _parked_task(app_engine, "t_tool_audit")
    token = current_user_id.set(_UID)
    try:
        await _pickup_tool(app_engine).execute(task_id=task.id)
    finally:
        current_user_id.reset(token)

    rows = _audit_rows(su_engine, task.id)
    assert len(rows) == 1
    assert rows[0]["user_id"] == _UID  # whose account it happened in
    assert rows[0]["metadata"]["via"] == "persona"  # and that the persona, not the user, did it


@pytest.mark.asyncio
async def test_another_tenants_task_is_out_of_reach_and_says_nothing_about_itself(
    app_engine: Engine, su_engine: Engine
) -> None:
    theirs = _parked_task(app_engine, "t_theirs", owner=_OTHER_UID, persona="persona_theirs")
    token = current_user_id.set(_UID)
    try:
        result = await _pickup_tool(app_engine).execute(task_id=theirs.id)
        nonsense = await _pickup_tool(app_engine).execute(task_id="task_does_not_exist")
    finally:
        current_user_id.reset(token)

    assert result.is_error is True
    assert result.content == "I don't have a task with id 't_theirs'."
    assert result.content.replace("t_theirs", "X") == nonsense.content.replace(
        "task_does_not_exist", "X"
    )  # the refusals are identical in shape: nothing leaks
    assert _jobs(su_engine, theirs.id) == []  # and nothing moved


@pytest.mark.asyncio
async def test_another_persona_of_the_same_owner_is_also_out_of_reach(
    app_engine: Engine, su_engine: Engine
) -> None:
    """Same owner, different persona: still not this persona's work to carry on."""
    sibling = _parked_task(app_engine, "t_sibling", persona=_OTHER_PERSONA)
    token = current_user_id.set(_UID)
    try:
        result = await _pickup_tool(app_engine).execute(task_id=sibling.id)
    finally:
        current_user_id.reset(token)

    assert result.is_error is True
    assert _jobs(su_engine, sibling.id) == []


@pytest.mark.asyncio
async def test_off_request_the_tool_cannot_act(app_engine: Engine, su_engine: Engine) -> None:
    """No bound caller (a background path, a bug): the port is not even BUILT.

    Two guards stand here, and the test distinguishes them on purpose. The provider refuses to
    build a port without a caller, and the port itself refuses to act without one. Asserting
    only "it refused" cannot tell them apart, so a mutation that built the port unscoped
    survived: the inner guard caught it and the test could not see the outer one had gone.
    The message is the evidence of WHICH guard held.
    """
    task = _parked_task(app_engine, "t_unscoped")
    result = await _pickup_tool(app_engine).execute(task_id=task.id)

    assert result.is_error is True
    assert "No task context" in result.content, "the PROVIDER must refuse before a port exists"
    assert _jobs(su_engine, task.id) == []


@pytest.mark.asyncio
async def test_a_paused_owner_stops_the_persona_exactly_as_it_stops_the_user(
    app_engine: Engine, su_engine: Engine
) -> None:
    """The pause is not a UI affordance, it is the guarantee. A persona route around it would
    make the dial a suggestion."""
    task = _parked_task(app_engine, "t_tool_paused")
    token = current_user_id.set(_UID)
    try:
        KillSwitchStore(app_engine).pause_owner(_UID, actor="user", now=datetime.now(UTC))
        result = await _pickup_tool(app_engine).execute(task_id=task.id)
        KillSwitchStore(app_engine).resume_owner(_UID, now=datetime.now(UTC))
    finally:
        current_user_id.reset(token)

    assert result.is_error is False  # a refusal, not a fault
    assert result.data["changed"] is False
    assert "autonomy is paused" in str(result.data["note"])
    assert _jobs(su_engine, task.id) == []


@pytest.mark.asyncio
async def test_a_finished_task_is_a_calm_no_op(app_engine: Engine, su_engine: Engine) -> None:
    task = _parked_task(app_engine, "t_tool_done")
    token = current_user_id.set(_UID)
    try:
        TaskStore(app_engine).cancel(_UID, task.id, now=datetime.now(UTC))
        result = await _pickup_tool(app_engine).execute(task_id=task.id)
    finally:
        current_user_id.reset(token)

    assert result.is_error is False  # finished is not a fault
    assert result.data["changed"] is False
    assert "finished" in str(result.data["note"])
    assert _jobs(su_engine, task.id) == []


@pytest.mark.asyncio
async def test_saying_pick_it_back_up_in_chat_reaches_the_same_seam(
    app_engine: Engine, su_engine: Engine
) -> None:
    """Spec W1 (T9): the steering cue reads "pick it back up" as RESUME, and a task waiting on
    the user is not paused, so the shared seam used to answer "Not paused." and do nothing.
    One door for "start it again", whichever way the user says it."""
    task = _parked_task(app_engine, "t_chat_pickup")
    token = current_user_id.set(_UID)
    try:
        outcome = task_control_service.resume_task(app_engine, _UID, task, now=datetime.now(UTC))
    finally:
        current_user_id.reset(token)

    assert outcome.changed is True
    # The queued leg IS the evidence: a pickup enqueues, and the handler performs the
    # waiting → active transition when it claims the job (the one resume point).
    assert _jobs(su_engine, task.id) == ["queued"]
    assert TaskStore(app_engine).get(_UID, task.id).state is TaskState.WAITING


@pytest.mark.asyncio
async def test_a_task_already_being_worked_is_refused(
    app_engine: Engine, su_engine: Engine
) -> None:
    """Pickup means "carry on something that stopped", not "run it again harder".

    A task that is ACTIVE has a leg of its own, and one parked on the CLOCK will wake by
    itself; picking either up would put a second leg against the same head. T9 hands this seam
    to a persona, which can ask for it repeatedly and without a user watching, so the refusal
    is pinned here rather than left to the seam's own tests.
    """
    active = _parked_task(app_engine, "t_active_already")
    scheduled = _parked_task(app_engine, "t_scheduled_already")
    token = current_user_id.set(_UID)
    try:
        store = TaskStore(app_engine)
        store.resume(_UID, active.id, now=datetime.now(UTC))  # back to ACTIVE
        # WAITING(on_user) → ACTIVE → WAITING(until_time): the clock will wake this one itself.
        store.resume(_UID, scheduled.id, now=datetime.now(UTC))
        store.begin_wait(_UID, scheduled.id, WaitKind.UNTIL_TIME, now=datetime.now(UTC))

        on_active = await _pickup_tool(app_engine).execute(task_id=active.id)
        on_scheduled = await _pickup_tool(app_engine).execute(task_id=scheduled.id)
    finally:
        current_user_id.reset(token)

    for result, task_id in ((on_active, active.id), (on_scheduled, scheduled.id)):
        assert result.is_error is False  # a refusal, not a fault
        assert result.data["changed"] is False
        assert "already being worked" in str(result.data["note"])
        assert _jobs(su_engine, task_id) == []  # and no second leg against the same head
