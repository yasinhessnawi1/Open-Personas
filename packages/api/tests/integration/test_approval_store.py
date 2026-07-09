"""ApprovalStore round-trip + the at-most-once CAS + one-pending idempotency (Spec A3, T6).

Runs the store under the ``persona_app`` non-superuser RLS engine (so a cross-tenant reach hits
zero rows). Concerns:

1. **Round-trip** — a proposal persists + reads back with its ``arguments`` value-exact (the
   replay payload, never re-derived) and its categories intact.
2. **The status CAS is at-most-once** — two transitions from the same ``expected`` state
   (a racing double-approve, and approve-vs-expire) → exactly one wins, the loser gets ``None``.
3. **One-pending-aware create** — a second pending proposal on a task returns the *existing*
   pending (idempotent), never a raw ``IntegrityError``.
4. **Decisions are an append-only audit trail**; **RLS isolation** holds (cross-tenant get
   misses).
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.approvals import ActionProposal, ApprovalDecision, DecisionType, ProposalStatus
from persona.errors import ApprovalNotFoundError
from persona.tools import ActionCategory
from persona_api.approvals import ApprovalStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_CONTRACT = '\'{"goal": "book the cheapest fare"}\''


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS store test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(engine: Engine, user: str, persona: str, task: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": user, "e": f"{user}@example.com"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
            {"p": persona, "o": user},
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, persona_id, contract_json) "
                f"VALUES (:t, :o, :p, {_CONTRACT}::jsonb)"
            ),
            {"t": task, "o": user, "p": persona},
        )


def _proposal(
    *, pid: str, owner: str, task: str, persona: str, args: dict[str, object] | None = None
) -> ActionProposal:
    return ActionProposal(
        proposal_id=pid,
        owner_id=owner,
        task_id=task,
        persona_id=persona,
        categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
        tool_name="send_email",
        arguments=args if args is not None else {"to": "bob@example.com", "subject": "hi"},
        description="Send an email to bob@example.com",
        created_at=datetime.now(UTC),
    )


# --- round-trip -------------------------------------------------------------


def test_create_and_get_round_trip_args_value_exact(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    store = ApprovalStore(app_engine)
    args = {"to": "bob@example.com", "amount": 1500, "cc": ["x@y.com"], "meta": {"k": 1}}
    created = store.create_proposal(
        _proposal(pid="p1", owner="user_a", task="t1", persona="persona_a", args=args)
    )
    fetched = store.get_proposal("user_a", "p1")
    assert created == fetched
    assert fetched.arguments == args  # value-exact replay payload
    assert fetched.categories == {ActionCategory.COMMUNICATE_AS_USER}
    assert fetched.status is ProposalStatus.PENDING


def test_get_missing_raises(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    store = ApprovalStore(app_engine)
    with pytest.raises(ApprovalNotFoundError):
        store.get_proposal("user_a", "nope")


def test_get_pending_for_task(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    store = ApprovalStore(app_engine)
    assert store.get_pending_for_task("user_a", "t1") is None
    store.create_proposal(_proposal(pid="p1", owner="user_a", task="t1", persona="persona_a"))
    pending = store.get_pending_for_task("user_a", "t1")
    assert pending is not None
    assert pending.proposal_id == "p1"


def test_list_pending_for_owner_across_tasks_and_rls_scoped(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """The A6 inbox read: every pending proposal across the owner's tasks, oldest-first (RLS)."""
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    _seed(migrated_engine, "user_b", "persona_b", "t3")  # another tenant
    with migrated_engine.begin() as conn:  # a second task for user_a
        conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, persona_id, contract_json) "
                f"VALUES ('t2', 'user_a', 'persona_a', {_CONTRACT}::jsonb)"
            )
        )
    store = ApprovalStore(app_engine)
    store.create_proposal(_proposal(pid="p1", owner="user_a", task="t1", persona="persona_a"))
    store.create_proposal(_proposal(pid="p2", owner="user_a", task="t2", persona="persona_a"))
    store.create_proposal(_proposal(pid="p3", owner="user_b", task="t3", persona="persona_b"))

    pending = store.list_pending_for_owner("user_a")
    # both of user_a's, oldest-first, and NOT user_b's (RLS scopes the read).
    assert [p.proposal_id for p in pending] == ["p1", "p2"]


def test_get_pending_for_conversation(migrated_engine: Engine, app_engine: Engine) -> None:
    """The A6 chat-twin reader: the pending proposal on a conversation's waiting(on_user) task."""
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES ('c1','user_a',:p)"),
            {"p": "persona_a"},
        )
        conn.execute(
            text(
                "UPDATE tasks SET conversation_id='c1', state='waiting', wait_kind='on_user' "
                "WHERE id='t1'"
            )
        )
    store = ApprovalStore(app_engine)
    assert store.get_pending_for_conversation("user_a", "c1") is None  # nothing pending yet
    store.create_proposal(_proposal(pid="p1", owner="user_a", task="t1", persona="persona_a"))

    found = store.get_pending_for_conversation("user_a", "c1")
    assert found is not None
    assert found.proposal_id == "p1"
    assert store.get_pending_for_conversation("user_a", "c_other") is None  # wrong conversation

    # once the task leaves waiting(on_user), the reader no longer matches (defensive qualifier).
    with migrated_engine.begin() as conn:
        conn.execute(text("UPDATE tasks SET state='active', wait_kind=NULL WHERE id='t1'"))
    assert store.get_pending_for_conversation("user_a", "c1") is None


# --- one-pending-aware create (idempotent, no raw IntegrityError) -----------


def test_second_pending_returns_existing(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    store = ApprovalStore(app_engine)
    store.create_proposal(_proposal(pid="p1", owner="user_a", task="t1", persona="persona_a"))
    # A second open proposal on the same task → the existing pending, not an IntegrityError.
    existing = store.create_proposal(
        _proposal(pid="p2", owner="user_a", task="t1", persona="persona_a")
    )
    assert existing.proposal_id == "p1"
    with app_engine.connect() as conn:
        conn.execute(text("SET app.current_user_id = 'user_a'"))
        count = conn.execute(
            text("SELECT count(*) FROM approval_proposals WHERE task_id='t1'")
        ).scalar_one()
    assert count == 1  # the second was never inserted


# --- the at-most-once status CAS --------------------------------------------


def test_cas_only_one_transition_from_pending_wins(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    store = ApprovalStore(app_engine)
    store.create_proposal(_proposal(pid="p1", owner="user_a", task="t1", persona="persona_a"))
    now = datetime.now(UTC)
    # Two attempts from pending → approved: only the first finds status='pending'.
    first = store.transition_proposal(
        "user_a", "p1", expected=ProposalStatus.PENDING, new=ProposalStatus.APPROVED, now=now
    )
    second = store.transition_proposal(
        "user_a", "p1", expected=ProposalStatus.PENDING, new=ProposalStatus.APPROVED, now=now
    )
    assert first is not None
    assert first.status is ProposalStatus.APPROVED
    assert second is None  # the CAS guard rejected the second — no double-execution


def test_cas_approve_vs_expire_race_one_winner(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    store = ApprovalStore(app_engine)
    store.create_proposal(_proposal(pid="p1", owner="user_a", task="t1", persona="persona_a"))
    now = datetime.now(UTC)
    approved = store.transition_proposal(
        "user_a", "p1", expected=ProposalStatus.PENDING, new=ProposalStatus.APPROVED, now=now
    )
    expired = store.transition_proposal(
        "user_a", "p1", expected=ProposalStatus.PENDING, new=ProposalStatus.EXPIRED, now=now
    )
    # Exactly one transition from pending wins; the other no-ops (the approve-vs-expire race).
    assert approved is not None
    assert expired is None
    assert store.get_proposal("user_a", "p1").status is ProposalStatus.APPROVED


def test_consumed_terminal_after_approve(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    store = ApprovalStore(app_engine)
    store.create_proposal(_proposal(pid="p1", owner="user_a", task="t1", persona="persona_a"))
    now = datetime.now(UTC)
    store.transition_proposal(
        "user_a", "p1", expected=ProposalStatus.PENDING, new=ProposalStatus.APPROVED, now=now
    )
    consumed = store.transition_proposal(
        "user_a", "p1", expected=ProposalStatus.APPROVED, new=ProposalStatus.CONSUMED, now=now
    )
    assert consumed is not None
    assert consumed.status is ProposalStatus.CONSUMED
    # A second consume from approved is rejected (the payload runs at most once).
    again = store.transition_proposal(
        "user_a", "p1", expected=ProposalStatus.APPROVED, new=ProposalStatus.CONSUMED, now=now
    )
    assert again is None


# --- decisions (append-only audit trail) + RLS ------------------------------


def _decision(did: str, proposal: str, dtype: DecisionType, reply: str) -> ApprovalDecision:
    return ApprovalDecision(
        decision_id=did,
        proposal_id=proposal,
        type=dtype,
        verbatim_reply=reply,
        channel="telegram",
        decided_at=datetime.now(UTC),
    )


def test_decisions_are_append_only(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a", "t1")
    store = ApprovalStore(app_engine)
    store.create_proposal(_proposal(pid="p1", owner="user_a", task="t1", persona="persona_a"))
    store.record_decision("user_a", _decision("d1", "p1", DecisionType.CLARIFY, "why?"))
    store.record_decision("user_a", _decision("d2", "p1", DecisionType.APPROVE, "yes send it"))
    decisions = store.list_decisions("user_a", "p1")
    assert [d.type for d in decisions] == [DecisionType.CLARIFY, DecisionType.APPROVE]
    assert decisions[1].verbatim_reply == "yes send it"


def test_cross_tenant_get_misses(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed(migrated_engine, "user_a", "persona_a", "ta")
    _seed(migrated_engine, "user_b", "persona_b", "tb")
    store = ApprovalStore(app_engine)
    store.create_proposal(_proposal(pid="pa", owner="user_a", task="ta", persona="persona_a"))
    # user_b cannot see user_a's proposal under RLS.
    with pytest.raises(ApprovalNotFoundError):
        store.get_proposal("user_b", "pa")
