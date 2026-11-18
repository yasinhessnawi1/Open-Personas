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
    ArtifactPointer,
    CheckpointKind,
    Contract,
    ContractBounds,
    CostLedger,
    Decision,
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
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import personas as personas_t
from persona_api.jobs.queue import JobQueue
from persona_api.routes import tasks as tasks_route
from persona_api.services import task_control_service
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.hold import (
    APPROVAL_HELD_AT_THE_CAP,
    APPROVAL_HELD_WHILE_PAUSED,
    APPROVAL_HOLD_FAILED,
    REPLY_RACED_NOTE,
    REPLY_SAVED_AT_THE_CAP_NOTE,
    REPLY_SAVED_BUT_NOT_HELD_NOTE,
    REPLY_TOO_LONG_AT_THE_CAP_NOTE,
    HoldOutcome,
    HoldReason,
    TaskHold,
)
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_OWNER = "user_held"
_PERSONA = "persona_held"
_CAP = 50_000
_QUESTION = "Which account should the deposit come from?"
_DECISION = Decision(
    decision="pay by bank transfer", rationale="the landlord takes no cards", leg_id="t1:leg:0"
)


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


class _Chain:
    """The task's checkpoint chain in the REAL store, read oldest first.

    The real ``CheckpointStore`` (its compare-and-set append included) runs on the community
    engine since the store mints the row id itself; ``append`` here is the seeding door.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def append(self, checkpoint: TaskCheckpoint) -> None:
        task = TaskStore(self._engine).get(_OWNER, "t1")
        CheckpointStore(self._engine).append(task, checkpoint, spend={}, now=_NOW)

    def _all(self) -> list[TaskCheckpoint]:
        recent = CheckpointStore(self._engine).list_recent(_OWNER, "t1", limit=100)
        return list(reversed(recent))

    def __getitem__(self, index: int) -> TaskCheckpoint:
        return self._all()[index]

    def __iter__(self) -> Iterator[TaskCheckpoint]:
        return iter(self._all())

    def __len__(self) -> int:
        return len(self._all())


@pytest.fixture
def chain(engine: Engine) -> _Chain:
    return _Chain(engine)


def _waiting_on_a_question(engine: Engine, chain: _Chain, *, spent: int) -> TaskStore:
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
            decisions=(_DECISION,),
            lessons=("the bank portal times out after five minutes",),
            queries_run=("deposit account norway",),
            sources_seen=("https://example.com/deposits",),
            current_plan=("confirm the account", "send the deposit"),
            next_step="confirm the account with the user",
            open_questions=(_QUESTION,),
            artifact_pointers=(ArtifactPointer(kind="workspace", ref="notes/deposit.md"),),
            event_log_cursor="run-1:7",
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
    engine: Engine, client: TestClient, enqueued: list[str], chain: _Chain
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
    # Everything the asking leg knew rides forward unchanged (D-W1-16), field by field.
    assert head.decisions == (_DECISION,)
    assert head.lessons == ("the bank portal times out after five minutes",)
    assert head.queries_run == ("deposit account norway",)
    assert head.sources_seen == ("https://example.com/deposits",)
    assert head.current_plan == ("confirm the account", "send the deposit")
    assert head.next_step == "confirm the account with the user"
    assert head.artifact_pointers == (ArtifactPointer(kind="workspace", ref="notes/deposit.md"),)
    assert head.event_log_cursor == "run-1:7"
    assert head.progress_conclusions[0] == "found the landlord's account details"


def test_raise_cap_carries_the_saved_reply_on_from_its_head(
    engine: Engine, client: TestClient, enqueued: list[str], chain: _Chain
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    _reply(client, "Use the joint account.")

    assert _extend(client, 10_000)

    assert enqueued == ["task:t1:after:1 revived"]  # one leg, from the saved reply
    assert _shape(store) == (TaskState.ACTIVE, None, False)


def test_a_second_reply_while_held_is_saved_too(
    engine: Engine, client: TestClient, enqueued: list[str], chain: _Chain
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
    engine: Engine, client: TestClient, enqueued: list[str], chain: _Chain
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
    engine: Engine, enqueued: list[str], chain: _Chain
) -> None:
    """The stranding shape: the task parked on the approval, then the user paused it."""
    store = _waiting_on_a_question(engine, chain, spent=0)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.APPROVE)

    assert (executor.calls, note, enqueued) == (1, APPROVAL_HELD_WHILE_PAUSED, [])
    assert _shape(store) == (TaskState.ACTIVE, None, True)
    # Resume now carries it on, from the decision's own checkpoint.
    task_control_service.resume_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    assert enqueued == ["task:t1:after:1 revived"]


@pytest.mark.asyncio
async def test_a_denial_answered_while_paused_is_held_the_same_way(
    engine: Engine, enqueued: list[str], chain: _Chain
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=0)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.DENY)

    assert (executor.calls, note, enqueued) == (0, APPROVAL_HELD_WHILE_PAUSED, [])
    assert _shape(store) == (TaskState.ACTIVE, None, True)
    assert checkpoint_kind("t1", chain[-1].leg_id) is CheckpointKind.APPROVAL


@pytest.mark.asyncio
async def test_an_approval_answered_at_the_cap_is_held_for_raise_cap(
    engine: Engine, client: TestClient, enqueued: list[str], chain: _Chain
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.APPROVE)

    assert (executor.calls, note, enqueued) == (1, APPROVAL_HELD_AT_THE_CAP, [])
    assert _shape(store) == (TaskState.ACTIVE, None, True)  # the budget pause
    assert _extend(client, 10_000)
    assert enqueued == ["task:t1:after:1 revived"]


@pytest.mark.asyncio
async def test_an_approval_on_a_runnable_task_still_resumes_at_once(
    engine: Engine, enqueued: list[str], chain: _Chain
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=0)
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.APPROVE)

    assert (executor.calls, note, enqueued) == (1, "", ["task:t1:after:1 user_reply"])
    assert _shape(store) == (TaskState.WAITING, WaitKind.ON_USER, False)


def test_the_reply_notes_are_human_and_carry_no_dashes() -> None:
    """The reply door's notes are shown as written; the approval notes are codes the web
    renders in its own copy (like the resolver's ``reconfirm``)."""
    from persona_api.tasks.hold import (
        REPLY_RACED_NOTE,
        REPLY_SAVED_BUT_NOT_HELD_NOTE,
        REPLY_TOO_LONG_AT_THE_CAP_NOTE,
    )

    for note in (
        REPLY_SAVED_AT_THE_CAP_NOTE,
        REPLY_TOO_LONG_AT_THE_CAP_NOTE,
        REPLY_RACED_NOTE,
        REPLY_SAVED_BUT_NOT_HELD_NOTE,
    ):
        assert chr(0x2014) not in note
        assert chr(0x2013) not in note
    assert "Raise cap" in REPLY_SAVED_AT_THE_CAP_NOTE
    assert "then answer" in REPLY_TOO_LONG_AT_THE_CAP_NOTE
    assert (APPROVAL_HELD_WHILE_PAUSED, APPROVAL_HELD_AT_THE_CAP) == ("held_paused", "held_at_cap")


# --- PR 34 review: the hold decides on what holds the task AFTER it moves it (B) ---------


def _between_reason_and_hold(
    monkeypatch: pytest.MonkeyPatch, competitor: Callable[[], object]
) -> None:
    """Let ``competitor`` land after the door read why the task is held, before the hold."""
    original = TaskHold.reason

    def _reason(self: TaskHold, owner_id: str, task: Task) -> HoldReason | None:
        why = original(self, owner_id, task)
        competitor()
        return why

    monkeypatch.setattr(TaskHold, "reason", _reason)


@pytest.mark.asyncio
async def test_a_resume_landing_before_the_hold_leaves_the_approved_task_running(
    engine: Engine, enqueued: list[str], chain: _Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval is answered on a paused task; the user's Resume lands while the task is
    still WAITING (so it only clears the pause and queues nothing); then the hold moves it to
    ACTIVE. It used to end ACTIVE, unpaused, with nothing queued. It now carries on."""
    store = _waiting_on_a_question(engine, chain, spent=0)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    _between_reason_and_hold(
        monkeypatch,
        lambda: task_control_service.resume_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW),
    )
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.APPROVE)

    assert (executor.calls, note) == (1, "")  # an ordinary approval that carried on
    assert enqueued == ["task:t1:after:1 revived"]
    assert _shape(store) == (TaskState.ACTIVE, None, False)


def test_a_raise_cap_landing_before_the_hold_carries_the_reply_on(
    engine: Engine,
    client: TestClient,
    enqueued: list[str],
    chain: _Chain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply at the cap; Raise cap lands before the hold (the task was WAITING, so Raise
    cap queued nothing); the hold's budget pause then finds no cap to pause at. It used to
    end ACTIVE, unpaused, with nothing queued. It now carries on, and says so truthfully."""
    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    from persona_api.approvals.budget import BudgetEnforcer

    raise_cap = BudgetEnforcer(engine=engine, tasks=store, queue=JobQueue(engine))
    _between_reason_and_hold(monkeypatch, lambda: raise_cap.extend(_OWNER, "t1", 10_000, now=_NOW))

    assert _reply(client, "Use the joint account.") == (200, True, "")

    assert enqueued == ["task:t1:after:1 revived"]
    assert _shape(store) == (TaskState.ACTIVE, None, False)
    assert checkpoint_kind("t1", chain[-1].leg_id) is CheckpointKind.REPLY


# --- the review's M2: a saved reply holds a second one only at the cap ---------------------


def test_a_reply_to_a_paused_task_on_a_saved_reply_under_the_cap_is_refused(
    engine: Engine, client: TestClient, enqueued: list[str], chain: _Chain
) -> None:
    """The reply was saved at the cap, then the cap was lifted without lifting the pause (an
    extension recorded outside Raise cap). The task is paused, not at its cap, and its head is
    a reply: a further reply is input to a paused task, refused as D-W1-10 refuses it."""
    _waiting_on_a_question(engine, chain, spent=_CAP)
    _reply(client, "Use the joint account.")
    with engine.begin() as conn:
        conn.execute(
            insert(audit_log_t).values(
                id="audit_ext",
                user_id=_OWNER,
                action="budget.extended",
                target="t1",
                metadata={"amount_micros": "10000", "cap_micros": str(_CAP)},
            )
        )

    assert _reply(client, "Actually, my own account.") == (
        200,
        False,
        "This task is not waiting on you.",
    )
    assert len(chain) == 2  # nothing new written
    assert enqueued == []


# --- D: a hold that fails after the answer is durable ---------------------------------------


def _hold_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> HoldOutcome:
        raise RuntimeError("database went away")

    monkeypatch.setattr(TaskHold, "hold", _raise)


@pytest.mark.usefixtures("enqueued")
def test_a_hold_failing_after_the_reply_is_saved_answers_truthfully_and_a_retry_adds_nothing(
    engine: Engine,
    client: TestClient,
    chain: _Chain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _waiting_on_a_question(engine, chain, spent=_CAP)
    _hold_fails(monkeypatch)

    assert _reply(client, "Use the joint account.") == (200, True, REPLY_SAVED_BUT_NOT_HELD_NOTE)
    assert _reply(client, "Use the joint account.") == (200, True, REPLY_SAVED_BUT_NOT_HELD_NOTE)

    assert [checkpoint_kind("t1", c.leg_id) for c in chain] == [
        CheckpointKind.LEG,
        CheckpointKind.REPLY,  # once: the retry of the same reply wrote nothing new
    ]


def test_a_budget_pause_that_fails_falls_back_to_a_plain_pause(
    engine: Engine,
    client: TestClient,
    enqueued: list[str],
    chain: _Chain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task must not be left ACTIVE and unpaused at its cap with nothing queued."""
    from persona_api.approvals.budget import BudgetEnforcer

    store = _waiting_on_a_question(engine, chain, spent=_CAP)

    def _raise(*_a: object, **_k: object) -> bool:
        raise RuntimeError("row lock timed out")

    with monkeypatch.context() as patched:
        patched.setattr(BudgetEnforcer, "enforce", _raise)
        assert _reply(client, "Use the joint account.") == (
            200,
            True,
            REPLY_SAVED_AT_THE_CAP_NOTE,
        )
    assert _shape(store) == (TaskState.ACTIVE, None, True)
    assert _extend(client, 10_000)
    assert enqueued == ["task:t1:after:1 revived"]


@pytest.mark.asyncio
async def test_an_approval_whose_hold_fails_says_so_instead_of_an_error(
    engine: Engine, enqueued: list[str], chain: _Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=0)
    task_control_service.pause_task(engine, _OWNER, store.get(_OWNER, "t1"), now=_NOW)
    _hold_fails(monkeypatch)
    executor = _Executor()

    note = await _decide(engine, executor, InboxDecision.APPROVE)

    assert (executor.calls, note, enqueued) == (1, APPROVAL_HOLD_FAILED, [])


# --- E: a reply the real store did not take is not called saved ------------------------------


@pytest.mark.usefixtures("enqueued")
def test_a_second_answer_racing_the_first_is_not_called_saved(
    engine: Engine, client: TestClient, chain: _Chain
) -> None:
    """A double submit of two different answers: both doors read the task WAITING at head 0.
    The first saves its reply at head 1; the second's append is a compare-and-set miss on the
    real store. It used to answer "Your reply is saved" for an answer nothing kept."""
    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    stale = store.get(_OWNER, "t1")
    assert _reply(client, "Use the joint account.") == (200, True, REPLY_SAVED_AT_THE_CAP_NOTE)

    outcome = task_control_service.reply_to_task(
        engine, _OWNER, stale, "Actually, my own account.", now=_NOW
    )

    assert (outcome.changed, outcome.note) == (False, REPLY_RACED_NOTE)
    assert len(chain) == 2
    assert "Use the joint account." in chain[-1].progress_conclusions[-1]


@pytest.mark.usefixtures("enqueued")
def test_a_double_submit_of_the_same_answer_is_saved_once(
    engine: Engine, client: TestClient, chain: _Chain
) -> None:
    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    stale = store.get(_OWNER, "t1")
    _reply(client, "Use the joint account.")

    outcome = task_control_service.reply_to_task(
        engine, _OWNER, stale, "Use the joint account.", now=_NOW
    )

    assert (outcome.changed, outcome.note) == (True, REPLY_SAVED_AT_THE_CAP_NOTE)
    assert len(chain) == 2


# --- H: a reply too long to keep at the cap (D-ORCH-11-18-e (2)) ----------------------------


def test_a_reply_too_long_to_keep_at_the_cap_is_refused_with_nothing_changed(
    engine: Engine,
    client: TestClient,
    enqueued: list[str],
    chain: _Chain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply record would exceed the checkpoint token budget. No 500, no partial write:
    the door answers "raise the cap, then answer", and the task is exactly as it was."""
    import functools

    from persona_api.tasks import hold as hold_module

    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    monkeypatch.setattr(
        hold_module, "CheckpointStore", functools.partial(CheckpointStore, token_budget=5)
    )

    assert _reply(client, "Use the joint account.") == (
        200,
        False,
        REPLY_TOO_LONG_AT_THE_CAP_NOTE,
    )
    assert len(chain) == 1
    assert _shape(store) == (TaskState.WAITING, WaitKind.ON_USER, False)
    assert enqueued == []


def test_the_same_answer_saved_concurrently_counts_as_saved_once(
    engine: Engine, chain: _Chain, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two submits of the same answer pass the idempotency read together, then race to the
    append: the loser's compare-and-set misses, it finds its own answer at the head, and it is
    reported as already saved rather than as a lost race."""
    from persona_api.tasks.hold import SavedReply

    store = _waiting_on_a_question(engine, chain, spent=_CAP)
    snapshot = store.get(_OWNER, "t1")
    original = CheckpointStore.append_applied
    fired = False

    def _the_other_submit_lands_first(
        self: CheckpointStore, task: Task, checkpoint: TaskCheckpoint, **kwargs: object
    ) -> tuple[Task, bool]:
        nonlocal fired
        if not fired:
            fired = True
            original(self, task, checkpoint, **kwargs)  # type: ignore[arg-type]
        return original(self, task, checkpoint, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(CheckpointStore, "append_applied", _the_other_submit_lands_first)

    saved = TaskHold(engine).save_reply(_OWNER, snapshot, "Use the joint account.", now=_NOW)

    assert saved is SavedReply.ALREADY_SAVED
    assert len(chain) == 2
