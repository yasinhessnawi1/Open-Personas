"""An answer that arrives when the task cannot run is kept, and the next leg waits (R9-240, R9-239).

Two doors answer a task that waits on the user, and both queued a leg whatever the task's
state:

- **A reply at the budget cap (R9-240).** The leg was boxed at zero remaining budget, stopped
  at once and paused again, and the user's answer rode a leg that did nothing with it.
  Refusing instead would lose what they typed. D-ORCH-11-18-d (3): the reply is saved as the
  task's head checkpoint (a ``reply`` record that answers the open question), the task takes
  the budget pause, and Raise cap carries it on from that head.
- **An approval answered while the task is paused, or at its cap (R9-239 part 1).** The
  resolver resumed unconditionally, so a paused task's leg was consumed at the claim; and a
  task that parked on the approval and was paused afterwards stayed WAITING, so Resume only
  cleared the pause and nothing ever ran again. D-ORCH-11-18-d (2): the decision is carried
  out at once, only the next leg waits, and the task moves to ACTIVE under its pause.

Either way the task ends ACTIVE and paused, which is exactly the shape Resume and Raise cap
already carry on from, so neither door needs a second way to restart work.

Driven through the real reply, resume and extend routes and the real resolver on the
community engine. Two Postgres-only pieces stand in: A0's enqueue is recorded rather than
inserted, and the checkpoint chain lives in memory (the community schema has no server
default for a checkpoint's row id).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona.approvals import ActionProposal, ProposalStatus
from persona.tasks import (
    CheckpointKind,
    Contract,
    ContractBounds,
    CostLedger,
    Task,
    TaskCheckpoint,
    TaskState,
    WaitKind,
    checkpoint_id,
    checkpoint_kind,
)
from persona.tools import ActionCategory
from persona_api.approvals import ApprovalResolver, ExecutedAction, InboxDecision
from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.db.models import tasks as tasks_t
from persona_api.jobs.queue import JobQueue
from persona_api.routes import tasks as tasks_route
from persona_api.services import task_control_service
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.hold import (
    APPROVAL_HELD_AT_THE_CAP_NOTE,
    APPROVAL_HELD_WHILE_PAUSED_NOTE,
    REPLY_SAVED_AT_THE_CAP_NOTE,
    TaskHold,
)
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import insert, update

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from persona.tasks import SpendKind
    from sqlalchemy import Engine

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_OWNER = "user_held"
_PERSONA = "persona_held"
_CAP = 50_000
_QUESTION = "Which account should the deposit come from?"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "held.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="h@example.com")
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


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The legs queued, as ``"{key} {trigger kind}"`` (A0's enqueue is Postgres-only)."""
    keys: list[str] = []

    def _enqueue(self: JobQueue, **kwargs: object) -> None:  # noqa: ARG001
        trigger = dict(kwargs["payload"])["trigger"]  # type: ignore[call-overload]
        keys.append(f"{kwargs['idempotency_key']} {trigger['kind']}")

    monkeypatch.setattr(JobQueue, "enqueue", _enqueue)
    return keys


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> list[TaskCheckpoint]:
    """The task's checkpoint chain, in memory, behind the real ``CheckpointStore`` API."""
    landed: list[TaskCheckpoint] = []

    def _latest(self: CheckpointStore, owner_id: str, task_id: str) -> TaskCheckpoint | None:  # noqa: ARG001
        return landed[-1] if landed else None

    def _recent(
        self: CheckpointStore,  # noqa: ARG001
        owner_id: str,  # noqa: ARG001
        task_id: str,  # noqa: ARG001
        *,
        limit: int,
    ) -> list[TaskCheckpoint]:
        return list(reversed(landed))[:limit]

    def _append(
        self: CheckpointStore,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int] | None = None,  # noqa: ARG001
        now: datetime,
    ) -> Task:
        assert checkpoint.checkpoint_seq == task.next_checkpoint_seq  # the CAS, in spirit
        landed.append(checkpoint)
        advanced = task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)
        _set_head(self._engine, advanced)
        return advanced

    monkeypatch.setattr(CheckpointStore, "get_latest", _latest)
    monkeypatch.setattr(CheckpointStore, "list_recent", _recent)
    monkeypatch.setattr(CheckpointStore, "append", _append)
    return landed


def _set_head(engine: Engine, task: Task) -> None:
    """Advance the durable head the way the real append's CAS does."""
    with engine.begin() as conn:
        conn.execute(
            update(tasks_t)
            .where(tasks_t.c.id == task.id)
            .values(head_checkpoint_seq=task.head_checkpoint_seq)
        )


def _waiting_on_a_question(engine: Engine, chain: list[TaskCheckpoint], *, spent: int) -> TaskStore:
    """A leg asked a question and parked: checkpoint 0 carries it, the task waits on the user."""
    store = TaskStore(engine)
    store.create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(
                goal="pay the deposit", bounds=ContractBounds(total_budget_micros=_CAP)
            ),
            state=TaskState.WAITING,
            wait_kind=WaitKind.ON_USER,
            head_checkpoint_seq=0,
            ledger=CostLedger(model_micros=spent),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    chain.append(
        TaskCheckpoint(
            task_id="t1",
            leg_id=checkpoint_id("t1", CheckpointKind.LEG, 0),
            checkpoint_seq=0,
            progress_conclusions=("found the landlord's account details",),
            open_questions=(_QUESTION,),
            queries_run=("deposit account norway",),
            updated_at=_NOW,
        )
    )
    return store


def _reply(client: TestClient, text: str) -> tuple[int, bool, str]:
    res = client.post("/v1/tasks/t1/reply", json={"reply": text})
    body = res.json()
    return (res.status_code, body["changed"], body["note"])


def _extend(client: TestClient, amount: int) -> bool:
    res = client.post("/v1/tasks/t1/budget/extend", json={"amount_micros": amount})
    return bool(res.json()["applied"])


def _shape(store: TaskStore) -> tuple[TaskState, WaitKind | None, bool]:
    task = store.get(_OWNER, "t1")
    return (task.state, task.wait_kind, task.paused)


# --- R9-240: a reply at the budget cap -------------------------------------------------


def test_a_reply_at_the_cap_is_saved_and_runs_nothing(
    engine: Engine, client: TestClient, enqueued: list[str], chain: list[TaskCheckpoint]
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=_CAP)

    assert _reply(client, "Use the joint account.") == (200, True, REPLY_SAVED_AT_THE_CAP_NOTE)

    assert enqueued == []  # no zero-budget leg
    assert _shape(store) == (TaskState.ACTIVE, None, True)  # held under the budget pause
    head = chain[-1]
    assert checkpoint_kind("t1", head.leg_id) is CheckpointKind.REPLY
    assert "Use the joint account." in head.progress_conclusions[-1]
    assert _QUESTION in head.progress_conclusions[-1]  # what it answers, in its own words
    assert head.open_questions == ()  # answered, so the next leg is not told it is open
    assert head.queries_run == ("deposit account norway",)  # the ledgers ride forward


def test_raise_cap_carries_the_saved_reply_on_from_its_head(
    engine: Engine, client: TestClient, enqueued: list[str], chain: list[TaskCheckpoint]
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    _reply(client, "Use the joint account.")

    assert _extend(client, 10_000)

    assert enqueued == ["task:t1:after:1 revived"]  # one leg, from the saved reply
    assert _shape(store) == (TaskState.ACTIVE, None, False)


def test_a_second_reply_while_held_is_saved_too(
    engine: Engine, client: TestClient, enqueued: list[str], chain: list[TaskCheckpoint]
) -> None:
    """Nothing typed is lost: a further answer before Raise cap is kept the same way."""
    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    _reply(client, "Use the joint account.")

    assert _reply(client, "Actually, my own account.") == (200, True, REPLY_SAVED_AT_THE_CAP_NOTE)

    assert enqueued == []
    assert _shape(store) == (TaskState.ACTIVE, None, True)
    assert [checkpoint_kind("t1", c.leg_id) for c in chain] == [
        CheckpointKind.LEG,
        CheckpointKind.REPLY,
        CheckpointKind.REPLY,
    ]
    assert "Actually, my own account." in chain[-1].progress_conclusions[-1]
    assert "Use the joint account." in chain[-1].progress_conclusions[-2]


def test_a_reply_under_the_cap_still_queues_its_leg_at_once(
    engine: Engine, client: TestClient, enqueued: list[str], chain: list[TaskCheckpoint]
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=_CAP - 1)

    assert _reply(client, "Use the joint account.") == (200, True, "")

    assert enqueued == ["task:t1:after:0 user_reply"]
    assert len(chain) == 1  # no record written: the leg carries the reply itself
    assert _shape(store) == (TaskState.WAITING, WaitKind.ON_USER, False)


# --- R9-239 part 1: an approval answered while the task cannot run ----------------------


class _Approvals:
    """The approval store's surface the resolver uses, for one pending proposal."""

    def __init__(self) -> None:
        self.proposal = ActionProposal(
            proposal_id="p1",
            owner_id=_OWNER,
            task_id="t1",
            persona_id=_PERSONA,
            categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
            tool_name="send_email",
            arguments={"to": "landlord@example.com"},
            description="Email the landlord the deposit receipt",
            created_at=_NOW,
        )

    def get_proposal(self, owner_id: str, proposal_id: str) -> ActionProposal:  # noqa: ARG002
        return self.proposal

    def list_decisions(self, owner_id: str, proposal_id: str) -> list[object]:  # noqa: ARG002
        return []

    def record_decision(self, owner_id: str, decision: object) -> None:  # noqa: ARG002
        return None

    def transition_proposal(
        self,
        owner_id: str,  # noqa: ARG002
        proposal_id: str,  # noqa: ARG002
        *,
        expected: ProposalStatus | frozenset[ProposalStatus],
        new: ProposalStatus,
        now: datetime,  # noqa: ARG002
    ) -> ActionProposal | None:
        allowed = expected if isinstance(expected, frozenset) else frozenset({expected})
        if self.proposal.status not in allowed:
            return None
        self.proposal = self.proposal.model_copy(update={"status": new})
        return self.proposal


class _Executor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> ExecutedAction:  # noqa: ARG002
        self.calls += 1
        return ExecutedAction(summary="sent")


class _Unused:
    """The interpreter and notifier: the structured inbox decision needs neither."""

    async def interpret(self, *_: object) -> object:  # pragma: no cover - never called
        raise AssertionError


def _resolver(engine: Engine, executor: _Executor) -> ApprovalResolver:
    tasks = TaskStore(engine)
    checkpoints = CheckpointStore(engine)
    return ApprovalResolver(
        approvals=_Approvals(),  # type: ignore[arg-type]
        tasks=tasks,
        checkpoints=checkpoints,
        continuation=TaskContinuation(
            task_store=tasks, queue=JobQueue(engine), checkpoint_store=checkpoints
        ),
        interpreter=_Unused(),  # type: ignore[arg-type]
        executor=executor,
        notifier=_Unused(),  # type: ignore[arg-type]
        hold=TaskHold(engine),
    )


async def _decide(engine: Engine, executor: _Executor, decision: InboxDecision) -> str:
    outcome = await _resolver(engine, executor).resolve_structured(
        _OWNER,
        "p1",
        decision=decision,
        verbatim_reply=decision.value,
        channel="web",
        now=_NOW,
    )
    return outcome.note


@pytest.mark.asyncio
async def test_an_approval_answered_after_a_pause_is_carried_out_and_waits_for_resume(
    engine: Engine, enqueued: list[str], chain: list[TaskCheckpoint]
) -> None:
    """The stranding shape: the task parked on the approval, then the user paused it."""
    store = _waiting_on_a_question(engine, chain, spent=0)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.APPROVE)

    assert (executor.calls, note, enqueued) == (1, APPROVAL_HELD_WHILE_PAUSED_NOTE, [])
    assert _shape(store) == (TaskState.ACTIVE, None, True)
    # Resume now carries it on, from the decision's own checkpoint.
    task_control_service.resume_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    assert enqueued == ["task:t1:after:1 revived"]


@pytest.mark.asyncio
async def test_a_denial_answered_while_paused_is_held_the_same_way(
    engine: Engine, enqueued: list[str], chain: list[TaskCheckpoint]
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=0)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.DENY)

    assert (executor.calls, note, enqueued) == (0, APPROVAL_HELD_WHILE_PAUSED_NOTE, [])
    assert _shape(store) == (TaskState.ACTIVE, None, True)
    assert checkpoint_kind("t1", chain[-1].leg_id) is CheckpointKind.APPROVAL


@pytest.mark.asyncio
async def test_an_approval_answered_at_the_cap_is_held_for_raise_cap(
    engine: Engine, client: TestClient, enqueued: list[str], chain: list[TaskCheckpoint]
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.APPROVE)

    assert (executor.calls, note, enqueued) == (1, APPROVAL_HELD_AT_THE_CAP_NOTE, [])
    assert _shape(store) == (TaskState.ACTIVE, None, True)  # the budget pause
    assert _extend(client, 10_000)
    assert enqueued == ["task:t1:after:1 revived"]


@pytest.mark.asyncio
async def test_an_approval_on_a_runnable_task_still_resumes_at_once(
    engine: Engine, enqueued: list[str], chain: list[TaskCheckpoint]
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=0)
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.APPROVE)

    assert (executor.calls, note, enqueued) == (1, "", ["task:t1:after:1 user_reply"])
    assert _shape(store) == (TaskState.WAITING, WaitKind.ON_USER, False)


def test_the_held_notes_are_human_and_carry_no_dashes() -> None:
    for note in (
        REPLY_SAVED_AT_THE_CAP_NOTE,
        APPROVAL_HELD_WHILE_PAUSED_NOTE,
        APPROVAL_HELD_AT_THE_CAP_NOTE,
    ):
        assert "\u2014" not in note
        assert "\u2013" not in note
    assert "Raise cap" in REPLY_SAVED_AT_THE_CAP_NOTE
    assert "Raise cap" in APPROVAL_HELD_AT_THE_CAP_NOTE
    assert "resume" in APPROVAL_HELD_WHILE_PAUSED_NOTE
