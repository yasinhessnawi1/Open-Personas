"""Unit tests for task outcome reports (Spec A2, T9).

The completion report reads the DURABLE ledger (not a re-derivation); the stuck-report is
honest (the real cause + the actual progress) and is a distinct type from completion, so a
failure can never be rendered as a success.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.tasks import (
    ArtifactPointer,
    Contract,
    CostLedger,
    Deliverable,
    DeliverableFormat,
    Task,
    TaskCheckpoint,
    build_cancellation_summary,
    build_completion_report,
    build_stuck_report,
)
from pydantic import ValidationError

_NOW = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)
_DELIVERABLE = Deliverable(format=DeliverableFormat.FILE, filename="fibre-brief.md")


def _task(*, contract: Contract | None = None) -> Task:
    return Task(
        id="t1",
        owner_id="user_a",
        persona_id="persona_a",
        contract=contract or Contract(goal="find the cheapest fare"),
        ledger=CostLedger(model_micros=1200, sandbox_micros=300),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _checkpoint() -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id="t1",
        leg_id="leg-3",
        checkpoint_seq=2,
        progress_conclusions=("best fare 1620kr (SAS, Tue)",),
        open_questions=("flexible on dates?",),
        next_step="book it",
        artifact_pointers=(ArtifactPointer(kind="workspace", ref="prices.csv"),),
        updated_at=_NOW,
    )


def test_completion_report_reads_the_durable_ledger() -> None:
    report = build_completion_report(_task(), _checkpoint(), now=_NOW)
    assert report.goal == "find the cheapest fare"
    assert report.conclusions == ("best fare 1620kr (SAS, Tue)",)
    assert report.artifacts[0].ref == "prices.csv"
    # cost from the durable ledger, not re-derived.
    assert report.model_micros == 1200
    assert report.sandbox_micros == 300
    assert report.total_micros == 1500
    assert report.completed_at == _NOW


def test_completion_report_first_leg_no_checkpoint() -> None:
    report = build_completion_report(_task(), None, now=_NOW)
    assert report.conclusions == ()
    assert report.artifacts == ()
    assert report.total_micros == 1500  # ledger still read


def test_stuck_report_is_honest() -> None:
    report = build_stuck_report(
        _task(), _checkpoint(), cause="external API 500 after 3 retries", now=_NOW
    )
    assert report.cause == "external API 500 after 3 retries"  # the real cause, not hidden
    assert report.where_it_stood == ("best fare 1620kr (SAS, Tue)",)
    assert report.open_questions == ("flexible on dates?",)
    assert report.next_step == "book it"
    assert report.total_micros == 1500


def test_stuck_report_no_progress() -> None:
    # A task that failed before any checkpoint → honest "no progress", real cause.
    report = build_stuck_report(_task(), None, cause="sandbox unavailable", now=_NOW)
    assert report.cause == "sandbox unavailable"
    assert report.where_it_stood == ()
    assert report.next_step == ""


def test_cancellation_summary() -> None:
    summary = build_cancellation_summary(_task(), _checkpoint(), now=_NOW)
    assert summary.where_it_stood == ("best fare 1620kr (SAS, Tue)",)
    assert summary.total_micros == 1500
    assert summary.cancelled_at == _NOW


def test_reports_are_frozen_and_distinct_types() -> None:
    completion = build_completion_report(_task(), _checkpoint(), now=_NOW)
    stuck = build_stuck_report(_task(), _checkpoint(), cause="x", now=_NOW)
    # distinct types — a stuck report is not a completion (honesty enforced at the type level).
    assert type(completion).__name__ == "CompletionReport"
    assert type(stuck).__name__ == "StuckReport"
    with pytest.raises(ValidationError):
        completion.goal = "y"  # type: ignore[misc]


# --- the agreed shape on the finished record (Spec W1, T11) ------------------


def test_the_completion_report_carries_the_shape_the_contract_agreed() -> None:
    """So the "I've finished" message can say what was produced and where, instead of
    leaving the user to guess whether the prose IS the deliverable or a note about it."""
    task = _task(contract=Contract(goal="brief me weekly", deliverable=_DELIVERABLE))
    report = build_completion_report(task, None, now=_NOW)

    assert report.deliverable == _DELIVERABLE
    assert report.deliverable.render() == "file in fibre-brief.md"


def test_a_contract_that_named_no_shape_reports_the_default() -> None:
    task = _task(contract=Contract(goal="brief me weekly"))
    report = build_completion_report(task, None, now=_NOW)

    assert report.deliverable.format is DeliverableFormat.FINDINGS_MARKDOWN
    assert report.deliverable.render() == "findings_markdown"
