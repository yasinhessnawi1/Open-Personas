"""The failure-honesty matrix — no failure path is silent (Spec A3, T13; criterion 7).

Pure-logic matrix (no DB): enumerate EVERY failure source and assert each produces a C0 account
with a real cause + at least one option + a cadence-bypass priority (so the cap can never suppress
it). The exhaustiveness gate (``all_failure_kinds_have_a_builder``) makes a future failure kind
without an account fail this test — the no-silent-failure property as a closed, self-maintaining
set.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.approvals import ActionProposal
from persona.tasks import StuckReport
from persona.tools import ActionCategory
from persona_api.approvals import (
    FailureAccount,
    FailureKind,
    account_for_budget_pause,
    account_for_cancel_failure,
    account_for_expired_approval,
    account_for_origination_failure,
    account_for_stuck,
    all_failure_kinds_have_a_builder,
    bypasses_cap,
)

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _stuck_report() -> StuckReport:
    return StuckReport(
        task_id="t1",
        cause="the booking site rejected every candidate fare",
        where_it_stood=("searched 12 fares", "none under 2000kr"),
        open_questions=("raise the budget?",),
        next_step="wait for the user's budget decision",
        total_micros=500,
        stuck_at=_NOW,
    )


def _proposal() -> ActionProposal:
    return ActionProposal(
        proposal_id="p1",
        owner_id="user_a",
        task_id="t1",
        persona_id="persona_a",
        categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
        tool_name="send_email",
        arguments={"to": "bob@example.com"},
        description="send the appeal email to bob@example.com",
        created_at=_NOW,
    )


def _all_accounts() -> dict[FailureKind, FailureAccount]:
    """Build a representative account for every failure path (the matrix)."""
    return {
        FailureKind.LEG_DEAD_LETTER: account_for_stuck(
            _stuck_report(), kind=FailureKind.LEG_DEAD_LETTER
        ),
        FailureKind.TASK_STUCK: account_for_stuck(_stuck_report(), kind=FailureKind.TASK_STUCK),
        FailureKind.BUDGET_PAUSE: account_for_budget_pause(
            "t1", cap_micros=1000, spent_micros=1000
        ),
        FailureKind.EXPIRED_APPROVAL: account_for_expired_approval(_proposal()),
        FailureKind.ORIGINATION_FAILED: account_for_origination_failure(
            "t1", cause="the database was unavailable when I tried to set it up"
        ),
        FailureKind.CANCEL_FAILED: account_for_cancel_failure(
            "t1", cause="the store was unavailable when I tried to cancel it"
        ),
    }


# --- the matrix: every failure path produces a non-silent, un-suppressable account ----------


@pytest.mark.parametrize("kind", list(FailureKind))
def test_every_failure_path_has_an_honest_account(kind: FailureKind) -> None:
    account = _all_accounts()[kind]
    assert account.kind is kind
    assert account.cause.strip()  # a real cause, never silence
    assert account.options  # at least one next step, never a dead end
    assert account.task_id  # the user can act on it
    # The account can NEVER be suppressed by the cadence cap (criterion 7 + T12 priority bypass).
    assert bypasses_cap(account.priority)


def test_failure_kind_set_is_exhaustively_covered() -> None:
    # The completeness gate: every FailureKind has a builder, and the matrix builds one for each.
    assert all_failure_kinds_have_a_builder()
    assert set(_all_accounts()) == set(FailureKind)


# --- the honesty guards (a builder cannot emit a silent / dead-end account) ------------------


def test_account_rejects_empty_cause() -> None:
    with pytest.raises(ValueError, match="non-empty cause"):
        FailureAccount(
            kind=FailureKind.TASK_STUCK,
            task_id="t1",
            headline="x",
            cause="   ",
            options=("cancel",),
            priority=account_for_budget_pause("t1", cap_micros=1, spent_micros=1).priority,
        )


def test_account_rejects_no_options() -> None:
    with pytest.raises(ValueError, match="at least one option"):
        FailureAccount(
            kind=FailureKind.TASK_STUCK,
            task_id="t1",
            headline="x",
            cause="a real cause",
            options=(),
            priority=account_for_budget_pause("t1", cap_micros=1, spent_micros=1).priority,
        )


def test_stuck_account_carries_the_real_cause() -> None:
    account = account_for_stuck(_stuck_report(), kind=FailureKind.LEG_DEAD_LETTER)
    assert "rejected every candidate fare" in account.cause  # the real cause, not a paraphrase
    assert any("resume" in o for o in account.options)
    assert any("cancel" in o for o in account.options)
