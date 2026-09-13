"""What a failure's cause says about trying again (Spec W1, D-W1-8).

The classification decides whether a task is picked up on the owner's behalf, so the two
directions are not symmetric. Calling a deterministic failure transient spends the owner's
credits on work that cannot succeed. Calling a transient failure deterministic only means the
user is asked, which is the product's normal state. So the default is deterministic, and an
unrecognised cause is offered rather than retried.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.tasks import (
    TRANSIENT_RETRY_AFTER,
    Contract,
    Task,
    build_stuck_report,
    classify_retryable,
)

_NOW = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


def _task() -> Task:
    return Task(
        id="t1",
        owner_id="user_a",
        persona_id="persona_a",
        contract=Contract(goal="brief hacker news"),
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.parametrize(
    "cause",
    [
        "rate limit exceeded on every provider",
        "HTTP 429 from the model gateway",
        "provider at capacity, try later",
        "upstream temporarily unavailable (503)",
        "the request timed out after 60s",
        "empty completion from the model",
        "connection reset by peer",
        "every backend in MultiModelChatBackend exhausted",
    ],
)
def test_the_world_being_briefly_unavailable_is_worth_another_go(cause: str) -> None:
    assert classify_retryable(cause) is True


@pytest.mark.parametrize(
    "cause",
    [
        "task budget exhausted; refusing to overrun it",
        "tool send_email is not allowed by this persona's policy",
        "forbidden: the workspace path is outside the sandbox",
        "invalid contract: no acceptance criteria",
        "checkpoint too large for the budget",
    ],
)
def test_the_work_being_wrong_is_never_retried_on_its_own(cause: str) -> None:
    assert classify_retryable(cause) is False


def test_an_unrecognised_cause_is_offered_not_retried() -> None:
    # Fail closed: the user is asked, rather than the owner being billed for a guess.
    assert classify_retryable("something nobody has seen before") is False
    assert classify_retryable("") is False


def test_a_deterministic_marker_wins_over_a_transient_one() -> None:
    """ "Over budget after a timeout" is still over budget: retrying it burns credits for
    nothing, so the deterministic reading is the safe one."""
    assert classify_retryable("timed out, then the task budget was exhausted") is False


def test_the_report_carries_the_verdict_and_when_it_becomes_eligible() -> None:
    transient = build_stuck_report(_task(), None, cause="429 rate limit", now=_NOW)
    assert transient.retryable is True
    assert transient.retry_after == _NOW + TRANSIENT_RETRY_AFTER

    deterministic = build_stuck_report(_task(), None, cause="budget exhausted", now=_NOW)
    assert deterministic.retryable is False
    assert deterministic.retry_after is None  # there is no later moment at which it would work


def test_the_report_is_still_an_honest_failure_either_way() -> None:
    """Retryable is about what happens next, never about softening what happened: the cause is
    carried verbatim and the report is a StuckReport, not a disguised completion."""
    report = build_stuck_report(_task(), None, cause="429 rate limit", now=_NOW)
    assert report.cause == "429 rate limit"
    assert report.task_id == "t1"
