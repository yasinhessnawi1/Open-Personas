"""The approval loop end-to-end: verbatim replay + two-layer at-most-once + the floor (Spec A3, T8).

Drives the :class:`ApprovalResolver` against **real** stores (the proposal CAS + the checkpoint
CAS are the at-most-once spine — fakes would not exercise them), with fakes for the injected
edges (execution / interpretation / C0). The properties the spec exists for:

- **Verbatim proposal-replay** — on approval the *exact* recorded ``(tool_name, arguments)``
  executes (approve A → execute A, never A′); the model never re-derives.
- **Two-layer at-most-once** — a duplicated approval reply (the status pre-check) and a raced
  approve CAS each execute the action **once** and append **one** resolution checkpoint.
- **The floor is never bypassed** — deny / clarify-once-then-deny / material-modify-re-confirm
  all route through :func:`resolve_reply`.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime

import pytest
from persona.approvals import (
    ActionProposal,
    InterpretedIntent,
    ProposalStatus,
    RawInterpretation,
)
from persona.tasks import Contract, Task
from persona.tools import ActionCategory
from persona_api.approvals import ApprovalResolver, ApprovalStore, InboxDecision
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)
_ARGS = {"to": "bob@example.com", "subject": "the appeal", "amount": 1500}


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping resolver test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


class _FakeExecutor:
    """Records the verbatim payloads it was asked to replay."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, tool_name: str, arguments: Mapping) -> str:
        self.calls.append((tool_name, dict(arguments)))
        return "sent"


class _FakeInterpreter:
    """Returns a fixed model reading (the floor bounds it)."""

    def __init__(self, raw: RawInterpretation) -> None:
        self._raw = raw

    async def interpret(self, reply: str, proposal: ActionProposal) -> RawInterpretation:  # noqa: ARG002
        return self._raw


class _FakeNotifier:
    def __init__(self) -> None:
        self.asks = 0
        self.reconfirms = 0
        self.clarifies = 0

    async def ask(self, proposal: ActionProposal) -> None:  # noqa: ARG002
        self.asks += 1

    async def reconfirm(self, proposal: ActionProposal) -> None:  # noqa: ARG002
        self.reconfirms += 1

    async def clarify(self, proposal: ActionProposal) -> None:  # noqa: ARG002
        self.clarifies += 1


class _FakeQueue:
    """Records resume enqueues (the continuation's leg-enqueue seam)."""

    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    def enqueue(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


def _seed(engine: Engine, user: str, persona: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": user, "e": f"{user}@example.com"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
            {"p": persona, "o": user},
        )


def _waiting_task(tasks: TaskStore, *, owner: str, persona: str, task_id: str) -> None:
    """Create a task and park it ``waiting(on_user)`` (the gated-leg end state)."""
    tasks.create(
        Task(
            id=task_id,
            owner_id=owner,
            persona_id=persona,
            contract=Contract(goal="win the appeal"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start(owner, task_id, now=_NOW)
    from persona.tasks import WaitKind

    tasks.begin_wait(owner, task_id, WaitKind.ON_USER, now=_NOW)


def _proposal(owner: str, persona: str, task_id: str, pid: str = "p1") -> ActionProposal:
    return ActionProposal(
        proposal_id=pid,
        owner_id=owner,
        task_id=task_id,
        persona_id=persona,
        categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
        tool_name="send_email",
        arguments=_ARGS,
        description="Send an email to bob@example.com",
        created_at=_NOW,
    )


def _resolver(engine: Engine, interp: _FakeInterpreter, **extra: object) -> tuple:
    approvals = ApprovalStore(engine)
    tasks = TaskStore(engine)
    checkpoints = CheckpointStore(engine)
    queue = _FakeQueue()
    continuation = TaskContinuation(task_store=tasks, queue=queue, checkpoint_store=checkpoints)  # type: ignore[arg-type]
    executor = _FakeExecutor()
    notifier = _FakeNotifier()
    resolver = ApprovalResolver(
        approvals=approvals,
        tasks=tasks,
        checkpoints=checkpoints,
        continuation=continuation,
        interpreter=interp,
        executor=executor,
        notifier=notifier,
    )
    return resolver, approvals, checkpoints, executor, notifier, queue


def _approve() -> _FakeInterpreter:
    return _FakeInterpreter(RawInterpretation(intent=InterpretedIntent.APPROVE, confidence=0.95))


# --- verbatim proposal-replay -----------------------------------------------


async def test_approval_replays_exact_payload(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    resolver, approvals, checkpoints, executor, _n, queue = _resolver(app_engine, _approve())
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    outcome = await resolver.resolve("user_a", "p1", "yes send it", "telegram", now=_NOW)

    assert outcome.executed is True
    # Byte-match: the EXACT proposal payload executed — approve A → execute A, never A′.
    assert executor.calls == [("send_email", _ARGS)]
    # Folded into a resolution checkpoint (head advanced, the conclusion recorded).
    head = checkpoints.get_latest("user_a", "t1")
    assert head is not None
    assert any("Approved + executed" in c for c in head.progress_conclusions)
    # Proposal consumed (the at-most-once terminal); the task resumed.
    assert approvals.get_proposal("user_a", "p1").status is ProposalStatus.CONSUMED
    assert len(queue.enqueued) == 1


# --- two-layer at-most-once -------------------------------------------------


async def test_duplicated_reply_executes_once(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    resolver, approvals, checkpoints, executor, _n, queue = _resolver(app_engine, _approve())
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    first = await resolver.resolve("user_a", "p1", "yes", "telegram", now=_NOW)
    # The duplicated C1 reply — the status pre-check no-ops it (proposal is consumed).
    second = await resolver.resolve("user_a", "p1", "yes", "telegram", now=_NOW)

    assert first.executed is True
    assert second.executed is False
    assert len(executor.calls) == 1  # executed exactly once
    assert checkpoints.list_recent("user_a", "t1", limit=10).__len__() == 1  # one resolution cp
    assert len(queue.enqueued) == 1  # one resume


async def test_raced_approve_cas_executes_once(migrated_engine: Engine, app_engine: Engine) -> None:
    # The true-concurrency guard: two resolutions both past the status pre-check (both saw
    # pending) race the proposal CAS — exactly one wins and executes.
    _seed(migrated_engine, "user_a", "persona_a")
    resolver, approvals, checkpoints, executor, _n, _q = _resolver(app_engine, _approve())
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    proposal = approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    first = await resolver._execute_and_resume("user_a", proposal, _ARGS, "yes", _NOW)
    second = await resolver._execute_and_resume("user_a", proposal, _ARGS, "yes", _NOW)

    assert first.executed is True
    assert second.executed is False  # lost the CAS race — no second execution
    assert len(executor.calls) == 1
    assert len(checkpoints.list_recent("user_a", "t1", limit=10)) == 1


# --- the floor is never bypassed --------------------------------------------


async def test_denial_does_not_execute_and_resumes(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    deny = _FakeInterpreter(RawInterpretation(intent=InterpretedIntent.DENY, confidence=0.9))
    resolver, approvals, checkpoints, executor, _n, queue = _resolver(app_engine, deny)
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    outcome = await resolver.resolve("user_a", "p1", "nei, vent", "telegram", now=_NOW)

    assert outcome.executed is False
    assert executor.calls == []
    assert approvals.get_proposal("user_a", "p1").status is ProposalStatus.DENIED
    head = checkpoints.get_latest("user_a", "t1")
    assert head is not None
    assert any("denied" in c.lower() for c in head.progress_conclusions)
    assert len(queue.enqueued) == 1  # the leg resumes to adapt to the denial


async def test_ambiguous_clarifies_once_then_denies(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    ambiguous = _FakeInterpreter(
        RawInterpretation(intent=InterpretedIntent.AMBIGUOUS, confidence=0.2)
    )
    resolver, approvals, _cp, executor, notifier, _q = _resolver(app_engine, ambiguous)
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    first = await resolver.resolve("user_a", "p1", "hmm", "telegram", now=_NOW)
    second = await resolver.resolve("user_a", "p1", "kanskje", "telegram", now=_NOW)

    assert first.outcome.value == "clarify"
    assert notifier.clarifies == 1
    # The second ambiguous reply denies (clarify-once-then-deny) — never approves.
    assert second.outcome.value == "deny"
    assert executor.calls == []


async def test_material_modify_reconfirms_without_executing(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    modify = _FakeInterpreter(
        RawInterpretation(
            intent=InterpretedIntent.MODIFY,
            confidence=0.95,
            edited_arguments={**_ARGS, "amount": 3000},  # a material change (amount)
        )
    )
    resolver, approvals, _cp, executor, notifier, _q = _resolver(app_engine, modify)
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    await resolver.resolve("user_a", "p1", "yes but 3000kr", "telegram", now=_NOW)

    assert executor.calls == []  # a material edit never executes without re-confirmation
    assert notifier.reconfirms == 1
    proposal = approvals.get_proposal("user_a", "p1")
    assert proposal.status is ProposalStatus.PENDING  # still open, awaiting the re-confirm
    assert proposal.arguments["amount"] == 3000  # the payload was revised in place


async def test_immaterial_modify_executes_edited_payload(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a")
    edited = {**_ARGS, "subject": "the appeal (final)"}  # a phrasing change → immaterial
    modify = _FakeInterpreter(
        RawInterpretation(intent=InterpretedIntent.MODIFY, confidence=0.95, edited_arguments=edited)
    )
    resolver, approvals, _cp, executor, _n, _q = _resolver(app_engine, modify)
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    await resolver.resolve("user_a", "p1", "yes, tweak the subject", "telegram", now=_NOW)

    # The EDITED payload executes verbatim (the immaterial phrasing change).
    assert executor.calls == [("send_email", edited)]
    assert approvals.get_proposal("user_a", "p1").status is ProposalStatus.CONSUMED


# --- the A6 inbox twin: resolve_structured through the SAME floor (Spec A6, B3) -------------


async def test_structured_approve_replays_exact_payload(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The inbox APPROVE (no model) executes the EXACT recorded payload — twin of the NL approve."""
    _seed(migrated_engine, "user_a", "persona_a")
    resolver, approvals, _cp, executor, _n, _q = _resolver(app_engine, _approve())
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    out = await resolver.resolve_structured(
        "user_a",
        "p1",
        decision=InboxDecision.APPROVE,
        verbatim_reply="approve",
        channel="web",
        now=_NOW,
    )

    assert out.executed is True
    assert executor.calls == [("send_email", _ARGS)]  # verbatim — no model re-derivation
    assert approvals.get_proposal("user_a", "p1").status is ProposalStatus.CONSUMED


async def test_structured_material_modify_reconfirms_without_executing(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """A structured MODIFY still hits the floor: a material edit re-confirms, never executes."""
    _seed(migrated_engine, "user_a", "persona_a")
    resolver, approvals, _cp, executor, notifier, _q = _resolver(app_engine, _approve())
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    edited = {**_ARGS, "to": "eve@example.com"}  # a material change (recipient)
    out = await resolver.resolve_structured(
        "user_a",
        "p1",
        decision=InboxDecision.MODIFY,
        edited_arguments=edited,
        verbatim_reply="change recipient",
        channel="web",
        now=_NOW,
    )

    assert out.outcome is not None
    assert out.outcome.value == "modify"
    assert notifier.reconfirms == 1
    assert executor.calls == []  # the floor governs the inbox too — no bypass
    assert approvals.get_proposal("user_a", "p1").status is ProposalStatus.PENDING


async def test_dual_resolution_inbox_then_chat_resolves_once(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """Idempotent convergence: inbox approves, a later chat reply is a clean 'already handled'."""
    _seed(migrated_engine, "user_a", "persona_a")
    resolver, approvals, _cp, executor, _n, _q = _resolver(app_engine, _approve())
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    inbox = await resolver.resolve_structured(
        "user_a",
        "p1",
        decision=InboxDecision.APPROVE,
        verbatim_reply="approve",
        channel="web",
        now=_NOW,
    )
    chat = await resolver.resolve("user_a", "p1", "yes", "chat", now=_NOW)  # the twin, second

    assert inbox.executed is True
    assert chat.outcome is None  # reflects, never errors
    assert chat.note == "not_pending"
    assert executor.calls == [("send_email", _ARGS)]  # executed EXACTLY once


async def test_concurrent_chat_and_inbox_resolve_execute_once(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The race, run TRULY concurrently: both surfaces past the pre-check contend the CAS →
    exactly one winner executes, the loser reflects; one execution, one resolution checkpoint."""
    import asyncio

    _seed(migrated_engine, "user_a", "persona_a")
    resolver, approvals, checkpoints, executor, _n, _q = _resolver(app_engine, _approve())
    _waiting_task(TaskStore(app_engine), owner="user_a", persona="persona_a", task_id="t1")
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1"))

    inbox, chat = await asyncio.gather(
        resolver.resolve_structured(
            "user_a",
            "p1",
            decision=InboxDecision.APPROVE,
            verbatim_reply="approve",
            channel="web",
            now=_NOW,
        ),
        resolver.resolve("user_a", "p1", "yes", "chat", now=_NOW),
    )

    executed = [r for r in (inbox, chat) if r.executed]
    assert len(executed) == 1  # exactly one winner
    assert len(executor.calls) == 1  # one execution — the CAS held under concurrency
    assert len(checkpoints.list_recent("user_a", "t1", limit=10)) == 1  # one resolution checkpoint
    assert approvals.get_proposal("user_a", "p1").status is ProposalStatus.CONSUMED
