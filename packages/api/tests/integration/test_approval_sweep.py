"""The expiry + reminder sweep: remind-once, auto-pause, terminal-expiry (Spec A3, T9; A3-D-2).

Against real Postgres (the remind-once + expiry CAS are the at-most-once spine). Concerns:

1. **Remind-once at 24h** — a pending proposal in ``[24h, 72h)`` is reminded; a **double-sweep**
   reminds it only once (the ``reminded_at`` CAS).
2. **Auto-pause + terminal expiry at 72h** — a pending proposal past 72h is CAS'd ``expired``
   (terminal — it can never be approved afterwards) and its task is **paused**; a double-sweep
   expires it only once.
3. **The reminder window** — a proposal already past 72h skips the reminder and goes straight
   to expiry; a fresh proposal (< 24h) is left untouched.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from persona.approvals import ActionProposal, ProposalStatus
from persona.tasks import Contract, Task, WaitKind
from persona.tools import ActionCategory
from persona_api.approvals import ApprovalStore, ApprovalSweeper, ApprovalSweepRunner
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)  # proposal creation time
_AT_25H = _T0 + timedelta(hours=25)
_AT_73H = _T0 + timedelta(hours=73)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping sweep test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_waiting_task(engine: Engine, user: str, persona: str, task: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": user, "e": f"{user}@example.com"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: x')"),
            {"p": persona, "o": user},
        )
    tasks = TaskStore(engine)
    tasks.create(
        Task(
            id=task,
            owner_id=user,
            persona_id=persona,
            contract=Contract(goal="win the appeal"),
            created_at=_T0,
            updated_at=_T0,
        )
    )
    tasks.start(user, task, now=_T0)
    tasks.begin_wait(user, task, WaitKind.ON_USER, now=_T0)


def _proposal(
    owner: str, persona: str, task: str, pid: str, *, created_at: datetime
) -> ActionProposal:
    return ActionProposal(
        proposal_id=pid,
        owner_id=owner,
        task_id=task,
        persona_id=persona,
        categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
        tool_name="send_email",
        arguments={"to": "bob@example.com"},
        description="Send an email to bob@example.com",
        created_at=created_at,
    )


def _sweeper(migrated_engine: Engine, app_engine: Engine) -> ApprovalSweeper:
    # Privileged scan on the superuser engine (cross-tenant); per-proposal CAS on the RLS engine.
    return ApprovalSweeper(
        dispatch_engine=migrated_engine,
        approvals=ApprovalStore(app_engine),
        tasks=TaskStore(app_engine),
    )


# --- remind-once ------------------------------------------------------------


def test_reminder_fires_exactly_once(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_waiting_task(migrated_engine, "user_a", "persona_a", "t1")
    ApprovalStore(app_engine).create_proposal(
        _proposal("user_a", "persona_a", "t1", "p1", created_at=_T0)
    )
    sweeper = _sweeper(migrated_engine, app_engine)

    first = sweeper.sweep(now=_AT_25H)
    second = sweeper.sweep(now=_AT_25H)  # the double-sweep

    assert [p.proposal_id for p in first.reminded] == ["p1"]
    assert second.reminded == ()  # reminded exactly once (the reminded_at CAS)
    assert first.expired == ()
    assert second.expired == ()
    # Still pending (a reminder doesn't resolve it); reminded_at now set.
    assert ApprovalStore(app_engine).get_proposal("user_a", "p1").status is ProposalStatus.PENDING


# --- auto-pause + terminal expiry -------------------------------------------


def test_expiry_pauses_task_and_is_terminal(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_waiting_task(migrated_engine, "user_a", "persona_a", "t1")
    approvals = ApprovalStore(app_engine)
    approvals.create_proposal(_proposal("user_a", "persona_a", "t1", "p1", created_at=_T0))
    sweeper = _sweeper(migrated_engine, app_engine)

    first = sweeper.sweep(now=_AT_73H)
    second = sweeper.sweep(now=_AT_73H)  # the double-sweep

    assert [p.proposal_id for p in first.expired] == ["p1"]
    assert second.expired == ()  # expired exactly once
    # The proposal is terminal — expired, and cannot be approved afterwards.
    assert approvals.get_proposal("user_a", "p1").status is ProposalStatus.EXPIRED
    cannot_approve = approvals.transition_proposal(
        "user_a", "p1", expected=ProposalStatus.PENDING, new=ProposalStatus.APPROVED, now=_AT_73H
    )
    assert cannot_approve is None
    # The task is auto-paused (it stopped waiting silently).
    assert TaskStore(app_engine).get("user_a", "t1").paused is True


def test_double_sweep_one_reminder_one_expiry(migrated_engine: Engine, app_engine: Engine) -> None:
    # Two tasks: one in the reminder window (25h), one past expiry (73h, created earlier).
    _seed_waiting_task(migrated_engine, "user_a", "persona_a", "t_remind")
    _seed_waiting_task(migrated_engine, "user_b", "persona_b", "t_expire")
    approvals_a = ApprovalStore(app_engine)
    approvals_a.create_proposal(
        _proposal(
            "user_a", "persona_a", "t_remind", "p_remind", created_at=_AT_73H - timedelta(hours=48)
        )
    )  # 25h old at _AT_73H
    approvals_a.create_proposal(
        _proposal("user_b", "persona_b", "t_expire", "p_expire", created_at=_T0)
    )  # 73h old at _AT_73H
    sweeper = _sweeper(migrated_engine, app_engine)

    first = sweeper.sweep(now=_AT_73H)
    second = sweeper.sweep(now=_AT_73H)

    assert {p.proposal_id for p in first.reminded} == {"p_remind"}
    assert {p.proposal_id for p in first.expired} == {"p_expire"}
    # The double-sweep is a clean no-op — no second reminder, no second expiry.
    assert second.reminded == ()
    assert second.expired == ()


# --- the reminder window ----------------------------------------------------


def test_past_expiry_skips_reminder_goes_to_expiry(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_waiting_task(migrated_engine, "user_a", "persona_a", "t1")
    ApprovalStore(app_engine).create_proposal(
        _proposal("user_a", "persona_a", "t1", "p1", created_at=_T0)
    )
    result = _sweeper(migrated_engine, app_engine).sweep(now=_AT_73H)
    # A 73h-old proposal is past the reminder window → not reminded, straight to expiry.
    assert result.reminded == ()
    assert [p.proposal_id for p in result.expired] == ["p1"]


def test_fresh_proposal_untouched(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_waiting_task(migrated_engine, "user_a", "persona_a", "t1")
    ApprovalStore(app_engine).create_proposal(
        _proposal("user_a", "persona_a", "t1", "p1", created_at=_T0)
    )
    # Ten hours in — below the reminder threshold; nothing happens.
    result = _sweeper(migrated_engine, app_engine).sweep(now=_T0 + timedelta(hours=10))
    assert result.reminded == ()
    assert result.expired == ()
    assert ApprovalStore(app_engine).get_proposal("user_a", "p1").status is ProposalStatus.PENDING


# --- the worker runner: leader-gating + best-effort C0 voicing (GAP #1a wiring) ----------------


class _FakeLeader:
    """A stand-in for ``SchedulerLeader`` (its real advisory-lock election is separately tested)."""

    def __init__(self, *, is_leader: bool = True) -> None:
        self._is_leader = is_leader

    def try_become_leader(self) -> bool:
        return self._is_leader


class _SpyApprovalNotifier:
    """Records the proposals voiced, so the wiring test asserts the C0 path fired (once each).

    The call-log attributes are named distinctly from the ``remind`` / ``expired`` methods so an
    instance attribute never shadows the coroutine the runner invokes.
    """

    def __init__(self) -> None:
        self.reminded_ids: list[str] = []
        self.expired_ids: list[str] = []

    async def remind(self, proposal: ActionProposal) -> None:
        self.reminded_ids.append(proposal.proposal_id)

    async def expired(self, proposal: ActionProposal) -> None:
        self.expired_ids.append(proposal.proposal_id)


@pytest.mark.asyncio
async def test_runner_voices_expiry_and_pauses_and_is_idempotent(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    # A >72h proposal → the runner expires it + auto-pauses the task + voices ONE expiry C0.
    # A double run_once (the cadence firing twice) voices exactly once (the expiry CAS is spent).
    _seed_waiting_task(migrated_engine, "user_a", "persona_a", "t1")
    ApprovalStore(app_engine).create_proposal(
        _proposal("user_a", "persona_a", "t1", "p1", created_at=_T0)
    )
    spy = _SpyApprovalNotifier()
    runner = ApprovalSweepRunner(
        sweeper=_sweeper(migrated_engine, app_engine),
        leader=_FakeLeader(),  # type: ignore[arg-type]  # duck-typed try_become_leader
        notifier=spy,
    )

    await runner.run_once(now=_AT_73H)
    await runner.run_once(now=_AT_73H)  # the cadence double-fire

    assert spy.expired_ids == ["p1"]  # voiced exactly once
    assert spy.reminded_ids == []
    assert ApprovalStore(app_engine).get_proposal("user_a", "p1").status is ProposalStatus.EXPIRED
    assert TaskStore(app_engine).get("user_a", "t1").paused is True


@pytest.mark.asyncio
async def test_runner_voices_reminder_once(migrated_engine: Engine, app_engine: Engine) -> None:
    # A >24h (but <72h) proposal → reminded once; the double run_once does not re-voice.
    _seed_waiting_task(migrated_engine, "user_a", "persona_a", "t1")
    ApprovalStore(app_engine).create_proposal(
        _proposal("user_a", "persona_a", "t1", "p1", created_at=_T0)
    )
    spy = _SpyApprovalNotifier()
    runner = ApprovalSweepRunner(
        sweeper=_sweeper(migrated_engine, app_engine),
        leader=_FakeLeader(),  # type: ignore[arg-type]
        notifier=spy,
    )

    await runner.run_once(now=_AT_25H)
    await runner.run_once(now=_AT_25H)

    assert spy.reminded_ids == ["p1"]  # reminded exactly once
    assert spy.expired_ids == []
    assert ApprovalStore(app_engine).get_proposal("user_a", "p1").status is ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_runner_non_leader_is_a_no_op(migrated_engine: Engine, app_engine: Engine) -> None:
    # A follower (not the leader) neither sweeps nor voices — leader-gating holds.
    _seed_waiting_task(migrated_engine, "user_a", "persona_a", "t1")
    ApprovalStore(app_engine).create_proposal(
        _proposal("user_a", "persona_a", "t1", "p1", created_at=_T0)
    )
    spy = _SpyApprovalNotifier()
    runner = ApprovalSweepRunner(
        sweeper=_sweeper(migrated_engine, app_engine),
        leader=_FakeLeader(is_leader=False),  # type: ignore[arg-type]
        notifier=spy,
    )

    result = await runner.run_once(now=_AT_73H)

    assert result is None  # not the leader → nothing happened
    assert spy.expired_ids == []
    assert ApprovalStore(app_engine).get_proposal("user_a", "p1").status is ProposalStatus.PENDING


@pytest.mark.asyncio
async def test_runner_without_backend_still_expires_no_voice(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    # The memory-backend gate: notifier=None (community / keyless) still expires + auto-pauses;
    # the voiced C0 degrades to the persist-only floor (no voicer to call).
    _seed_waiting_task(migrated_engine, "user_a", "persona_a", "t1")
    ApprovalStore(app_engine).create_proposal(
        _proposal("user_a", "persona_a", "t1", "p1", created_at=_T0)
    )
    runner = ApprovalSweepRunner(
        sweeper=_sweeper(migrated_engine, app_engine),
        leader=_FakeLeader(),  # type: ignore[arg-type]
        notifier=None,
    )

    result = await runner.run_once(now=_AT_73H)

    assert result is not None
    assert [p.proposal_id for p in result.expired] == ["p1"]  # state change still happened
    assert ApprovalStore(app_engine).get_proposal("user_a", "p1").status is ProposalStatus.EXPIRED
    assert TaskStore(app_engine).get("user_a", "t1").paused is True
