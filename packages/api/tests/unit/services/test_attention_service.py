"""One attention query feeds the review list and the badge (Spec W1, T5; D-W1-5 / D-W1-6).

Drives the REAL ``list_attention`` / ``count_attention`` against a real database (the
community SQLite engine) through the REAL stores and the REAL dead-leg reaction. The fixture
the owner asked for is the one that pins equality: a task WAITING on the user (a question), a
DEAD-LETTERED task (parked by ``react_to_dead_leg``, the exact reaction the sweep performs,
against a real dead job row that carries the cause), a FAILED task, and a pending APPROVAL,
all for one owner, plus a working task that must NOT count.

What is pinned: the badge count is the list's length; every kind appears once (a task whose
wait is a pending approval is the approval, never a second row); the dead-lettered offer
carries the dead job's real cause; the actions follow the reason; another owner's rows never
leak; the terminal window bounds FAILED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.approvals import ActionProposal
from persona.tasks import Contract, Task, TaskState, WaitKind
from persona.tools import ActionCategory
from persona_api.approvals.store import ApprovalStore
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import personas as personas_t
from persona_api.jobs.queue import JobQueue
from persona_api.services import nav_counts_service
from persona_api.services.attention_service import (
    AttentionKind,
    AttentionReason,
    count_attention,
    list_attention,
)
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.store import CheckpointStore, TaskStore
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
_OWNER = "user_attn"
_OTHER = "user_other"
_PERSONA = "persona_attn"
_DEAD_CAUSE = "every backend in MultiModelChatBackend exhausted"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "attention.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    ensure_owner(eng, owner_id=_OTHER, email="o@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
        conn.execute(insert(personas_t).values(id="p_other", owner_id=_OTHER, yaml="name: y"))
    yield eng
    eng.dispose()


def _task(
    store: TaskStore, task_id: str, goal: str, *, owner: str = _OWNER, at: datetime = _NOW
) -> None:
    store.create(
        Task(
            id=task_id,
            owner_id=owner,
            persona_id=_PERSONA if owner == _OWNER else "p_other",
            contract=Contract(goal=goal),
            created_at=at,
            updated_at=at,
        )
    )
    store.start(owner, task_id, now=at)


def _dead_job(engine: Engine, task_id: str, *, owner: str = _OWNER) -> None:
    """A REAL dead-lettered leg job row (what A0 leaves after retries are exhausted)."""
    with engine.begin() as conn:
        conn.execute(
            insert(jobs_t).values(
                id=f"job-{task_id}",
                type="task_leg",
                owner_id=owner,
                payload={"task_id": task_id, "predecessor_seq": None},
                idempotency_key=f"task:{task_id}:after:init",
                state="dead",
                attempt=5,
                max_attempts=5,
                last_error=_DEAD_CAUSE,
            )
        )


def _continuation(engine: Engine) -> TaskContinuation:
    return TaskContinuation(
        task_store=TaskStore(engine),
        queue=JobQueue(engine),
        checkpoint_store=CheckpointStore(engine),
    )


@pytest.fixture
def seeded(engine: Engine) -> Engine:
    """The owner's fixture: waiting (question), dead-lettered, failed, approval, and a worker."""
    store = TaskStore(engine)
    # 1. waiting on the user with a question (parked by the leg's ask; no dead job).
    _task(store, "t_question", "book the dentist", at=_NOW - timedelta(hours=3))
    store.begin_wait(_OWNER, "t_question", WaitKind.ON_USER, now=_NOW - timedelta(hours=3))
    # 2. dead-lettered: a real dead job, then the sweep's reaction parks the task.
    _task(store, "t_dead", "brief hacker news", at=_NOW - timedelta(hours=2))
    _dead_job(engine, "t_dead")
    report = _continuation(engine).react_to_dead_leg(_OWNER, "t_dead", _DEAD_CAUSE, now=_NOW)
    assert report is not None
    # 3. failed.
    _task(store, "t_failed", "renew the parking permit", at=_NOW - timedelta(hours=1))
    store.fail(_OWNER, "t_failed", now=_NOW)
    # 4. an approval: the task waits on the user AND has a pending proposal → ONE row.
    _task(store, "t_approval", "reply to the landlord", at=_NOW - timedelta(hours=4))
    store.begin_wait(_OWNER, "t_approval", WaitKind.ON_USER, now=_NOW - timedelta(hours=4))
    ApprovalStore(engine).create_proposal(
        ActionProposal(
            proposal_id="p1",
            owner_id=_OWNER,
            task_id="t_approval",
            persona_id=_PERSONA,
            categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
            tool_name="send_email",
            arguments={"to": "landlord@example.com"},
            description="Reply to the landlord about the deposit",
            created_at=_NOW - timedelta(hours=4),
        )
    )
    # 5. a task being worked: NOT attention.
    _task(store, "t_working", "summarise the newsletters")
    # 6. a completed task: NOT attention.
    _task(store, "t_done", "file the receipts")
    store.complete(_OWNER, "t_done", now=_NOW)
    # 7. another owner's waiting task: never ours.
    _task(store, "t_theirs", "their thing", owner=_OTHER)
    store.begin_wait(_OTHER, "t_theirs", WaitKind.ON_USER, now=_NOW)
    return engine


# --- the equality the badge rests on -------------------------------------------------------


def test_the_badge_is_the_length_of_the_review_list(seeded: Engine) -> None:
    items = list_attention(seeded, owner_id=_OWNER, now=_NOW)
    assert count_attention(seeded, owner_id=_OWNER, now=_NOW) == len(items) == 4
    # The SERVED badge, through the nav-counts service the sidebar reads, on this same
    # engine: a run without Postgres also catches a decoupled badge (owner fold-in at T6).
    served = nav_counts_service.get_nav_counts(
        seeded, owner_id=_OWNER, include_memory=False, now=_NOW
    )
    assert served["attention"] == len(items)
    assert [i.kind for i in items] == [
        AttentionKind.APPROVAL,
        AttentionKind.WAITING_ON_USER,
        AttentionKind.WAITING_ON_USER,
        AttentionKind.FAILED,
    ]
    # Triage order: approvals, then the longest-waiting task first, then failed.
    assert [i.task_id for i in items] == ["t_approval", "t_question", "t_dead", "t_failed"]


def test_a_task_waiting_on_an_approval_is_listed_once_as_the_approval(seeded: Engine) -> None:
    items = list_attention(seeded, owner_id=_OWNER, now=_NOW)
    approvals = [i for i in items if i.kind is AttentionKind.APPROVAL]
    assert len(approvals) == 1
    assert approvals[0].proposal_id == "p1"
    assert approvals[0].task_id == "t_approval"
    assert approvals[0].actions == ("approve", "decline")
    assert [i.task_id for i in items].count("t_approval") == 1


def test_a_dead_lettered_task_is_an_offer_with_the_dead_jobs_real_cause(seeded: Engine) -> None:
    dead = next(
        i for i in list_attention(seeded, owner_id=_OWNER, now=_NOW) if i.task_id == "t_dead"
    )
    assert dead.kind is AttentionKind.WAITING_ON_USER
    assert dead.reason is AttentionReason.STUCK
    assert dead.detail == _DEAD_CAUSE
    assert dead.actions == ("pickup", "cancel")
    assert TaskStore(seeded).get(_OWNER, "t_dead").state is TaskState.WAITING


def test_a_plain_waiting_task_offers_reply_pickup_and_cancel(seeded: Engine) -> None:
    waiting = next(
        i for i in list_attention(seeded, owner_id=_OWNER, now=_NOW) if i.task_id == "t_question"
    )
    assert waiting.reason is AttentionReason.WAITING  # no checkpoint carries a question yet
    assert waiting.actions == ("reply", "pickup", "cancel")


def test_a_failed_task_inside_the_window_is_attention(seeded: Engine) -> None:
    failed = next(
        i for i in list_attention(seeded, owner_id=_OWNER, now=_NOW) if i.task_id == "t_failed"
    )
    assert failed.kind is AttentionKind.FAILED
    assert failed.actions == ("retry",)  # terminal: run again as a new task, never picked up


def test_working_and_done_tasks_are_not_attention(seeded: Engine) -> None:
    ids = {i.task_id for i in list_attention(seeded, owner_id=_OWNER, now=_NOW)}
    assert "t_working" not in ids
    assert "t_done" not in ids


def test_another_owners_rows_never_leak(seeded: Engine) -> None:
    mine = {i.task_id for i in list_attention(seeded, owner_id=_OWNER, now=_NOW)}
    theirs = {i.task_id for i in list_attention(seeded, owner_id=_OTHER, now=_NOW)}
    assert "t_theirs" not in mine
    assert theirs == {"t_theirs"}
    assert count_attention(seeded, owner_id=_OTHER, now=_NOW) == 1


def test_an_empty_account_needs_nothing(engine: Engine) -> None:
    assert list_attention(engine, owner_id=_OWNER, now=_NOW) == []
    assert count_attention(engine, owner_id=_OWNER, now=_NOW) == 0


def test_the_line_asks_the_question_the_task_is_parked_on(engine: Engine) -> None:
    """Spec W1 (D-W1-35): the line shows what is open NOW, never the first thing ever asked.

    A reply clears the question it answered, so a parked head normally carries exactly one.
    This pins the read anyway: given more than one, the line offers the most recent, because
    asking someone to answer a question they already answered is worse than saying nothing.
    """
    from persona.tasks import TaskCheckpoint
    from persona_api.db.models import task_checkpoints as checkpoints_t

    store = TaskStore(engine)
    _task(store, "t_two", "book the dentist")
    checkpoint = TaskCheckpoint(
        task_id="t_two",
        leg_id="t_two:leg:0",
        checkpoint_seq=0,
        progress_conclusions=(),
        next_step="",
        open_questions=("Which dentist?", "Morning or afternoon?"),
        updated_at=_NOW,
    )
    # The store's append is a Postgres-only CAS upsert, so the row is seeded directly here
    # (the community engine is SQLite); the reader still reads it through the real store.
    with engine.begin() as conn:
        conn.execute(
            insert(checkpoints_t).values(
                id="cp_two",
                task_id="t_two",
                owner_id=_OWNER,
                checkpoint_seq=0,
                checkpoint_json=checkpoint.model_dump(mode="json"),
                content_hash=checkpoint.content_hash,
            )
        )
    store.begin_wait(_OWNER, "t_two", WaitKind.ON_USER, now=_NOW)

    item = next(
        i for i in list_attention(engine, owner_id=_OWNER, now=_NOW) if i.task_id == "t_two"
    )
    assert item.reason is AttentionReason.QUESTION
    assert item.detail == "Morning or afternoon?"  # the one it is parked on, not the first
    assert item.actions == ("reply", "cancel")
