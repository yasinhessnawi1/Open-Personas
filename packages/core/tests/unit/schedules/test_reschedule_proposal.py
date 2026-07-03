"""A8 T4 — the persona-reschedule proposal record + dual-resolution idempotency (A8-D-12).

Pure, zero-infrastructure: the value-type invariants + the first-decision-wins resolution
rule the dual-resolution (chat now / A6 inbox later) design rests on.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.schedules import (
    RecurrenceFreq,
    RecurrenceRule,
    RescheduleProposal,
    RescheduleProposalStatus,
    resolve_proposal,
)

_NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _pending() -> RescheduleProposal:
    return RescheduleProposal(
        proposal_id="p1",
        owner_id="user_a",
        schedule_id="s1",
        persona_id="persona_1",
        proposed_recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(9,), byminute=(0,)),
        proposed_timezone="Europe/Oslo",
        human_terms="every day at 09:00 your time",
        reason="the 08:00 run keeps hitting your quiet hours",
        created_at=_NOW,
    )


def test_pending_proposal_carries_no_resolution() -> None:
    p = _pending()
    assert p.status is RescheduleProposalStatus.PENDING
    assert p.resolved_by is None
    assert p.resolved_at is None


def test_resolve_approve_confirms_with_reference() -> None:
    resolved = resolve_proposal(_pending(), approve=True, resolved_by="user_a", now=_NOW)
    assert resolved.status is RescheduleProposalStatus.CONFIRMED
    assert resolved.resolved_by == "user_a"  # the confirmation reference
    assert resolved.resolved_at == _NOW


def test_resolve_reject_marks_rejected() -> None:
    resolved = resolve_proposal(_pending(), approve=False, resolved_by="user_a", now=_NOW)
    assert resolved.status is RescheduleProposalStatus.REJECTED


def test_resolution_is_first_decision_wins_idempotent() -> None:
    # Chat confirms first; a later A6-inbox reject on the SAME record is a no-op (first wins).
    confirmed = resolve_proposal(_pending(), approve=True, resolved_by="chat", now=_NOW)
    later = datetime(2026, 1, 15, 13, 0, tzinfo=UTC)
    again = resolve_proposal(confirmed, approve=False, resolved_by="a6_inbox", now=later)
    assert again == confirmed  # unchanged — the change can never double-apply
    assert again.status is RescheduleProposalStatus.CONFIRMED
    assert again.resolved_by == "chat"


def test_proposed_recurrence_xor_one_time_enforced() -> None:
    with pytest.raises(ValueError, match="XOR"):
        RescheduleProposal(
            proposal_id="p1",
            owner_id="user_a",
            schedule_id="s1",
            persona_id="persona_1",
            proposed_recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(9,)),
            proposed_one_time=_NOW,  # both set → invalid
            proposed_timezone="Europe/Oslo",
            human_terms="...",
            reason="...",
            created_at=_NOW,
        )


def test_resolved_status_requires_reference() -> None:
    # A confirmed proposal with no resolved_by is unrepresentable (the reference is mandatory).
    with pytest.raises(ValueError, match="resolved_by"):
        RescheduleProposal(
            proposal_id="p1",
            owner_id="user_a",
            schedule_id="s1",
            persona_id="persona_1",
            proposed_recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(9,)),
            proposed_timezone="Europe/Oslo",
            human_terms="...",
            reason="...",
            status=RescheduleProposalStatus.CONFIRMED,
            created_at=_NOW,
        )
