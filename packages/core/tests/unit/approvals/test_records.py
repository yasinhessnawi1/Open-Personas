"""Tests for the A3 approval record value types + the materiality classifier (A3-D-3, T3).

The proposal is the **safety artifact** — the exact action, recorded verbatim for replay
(A3-D-X-approved-execution). The decision carries the user's verbatim reply + channel (the
audit trail). The materiality classifier draws the A3-D-3 line: a change to recipient /
amount / commitment re-confirms; a pure phrasing edit executes — and **defaults to material
on anything it doesn't recognise as phrasing** (the safe direction).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.approvals import (
    ActionProposal,
    ApprovalDecision,
    DecisionType,
    Materiality,
    ProposalStatus,
    classify_modification,
)
from persona.tools import ActionCategory
from pydantic import ValidationError


def _utc() -> datetime:
    return datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


class TestActionProposal:
    def _proposal(self, **overrides: object) -> ActionProposal:
        base: dict[str, object] = {
            "proposal_id": "prop_1",
            "task_id": "task_1",
            "owner_id": "owner_1",
            "persona_id": "persona_1",
            "categories": frozenset({ActionCategory.COMMUNICATE_AS_USER}),
            "tool_name": "send_email",
            "arguments": {"to": "bob@example.com", "subject": "hi", "body": "the draft"},
            "description": "Send an email to bob@example.com: hi",
            "created_at": _utc(),
        }
        base.update(overrides)
        return ActionProposal(**base)  # type: ignore[arg-type]

    def test_defaults_to_pending(self) -> None:
        assert self._proposal().status is ProposalStatus.PENDING

    def test_records_the_exact_payload(self) -> None:
        proposal = self._proposal()
        # The recorded args are exactly what replay executes (verbatim).
        assert proposal.arguments == {
            "to": "bob@example.com",
            "subject": "hi",
            "body": "the draft",
        }
        assert proposal.tool_name == "send_email"

    def test_is_frozen_and_forbids_extra(self) -> None:
        proposal = self._proposal()
        with pytest.raises(ValidationError):
            proposal.status = ProposalStatus.APPROVED  # type: ignore[misc]
        with pytest.raises(ValidationError):
            self._proposal(unexpected="x")

    def test_rejects_naive_created_at(self) -> None:
        with pytest.raises(ValidationError):
            self._proposal(created_at=datetime(2026, 6, 27, 12, 0))  # noqa: DTZ001 — testing rejection

    def test_round_trips_through_json(self) -> None:
        proposal = self._proposal()
        rebuilt = ActionProposal.model_validate(proposal.model_dump(mode="json"))
        assert rebuilt == proposal
        assert ActionCategory.COMMUNICATE_AS_USER in rebuilt.categories


class TestApprovalDecision:
    def _decision(self, **overrides: object) -> ApprovalDecision:
        base: dict[str, object] = {
            "decision_id": "dec_1",
            "proposal_id": "prop_1",
            "type": DecisionType.APPROVE,
            "verbatim_reply": "yes, send it",
            "channel": "telegram",
            "decided_at": _utc(),
        }
        base.update(overrides)
        return ApprovalDecision(**base)  # type: ignore[arg-type]

    def test_records_verbatim_reply_and_channel(self) -> None:
        decision = self._decision(verbatim_reply="nei, vent", type=DecisionType.DENY)
        assert decision.verbatim_reply == "nei, vent"
        assert decision.channel == "telegram"
        assert decision.type is DecisionType.DENY

    def test_modify_carries_edited_arguments(self) -> None:
        decision = self._decision(
            type=DecisionType.MODIFY, edited_arguments={"to": "alice@example.com"}
        )
        assert decision.edited_arguments == {"to": "alice@example.com"}

    def test_edited_arguments_only_valid_for_modify(self) -> None:
        # An approve/deny/clarify must not carry edits — guards against a confused decision.
        with pytest.raises(ValidationError):
            self._decision(type=DecisionType.APPROVE, edited_arguments={"to": "x"})

    def test_modify_requires_edited_arguments(self) -> None:
        with pytest.raises(ValidationError):
            self._decision(type=DecisionType.MODIFY)

    def test_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            self._decision().channel = "email"  # type: ignore[misc]


class TestDecisionAndStatusEnums:
    def test_decision_types(self) -> None:
        assert {d.value for d in DecisionType} == {"approve", "deny", "modify", "clarify"}

    def test_proposal_statuses(self) -> None:
        assert {s.value for s in ProposalStatus} == {
            "pending",
            "approved",
            "denied",
            "modified",
            "expired",
            "consumed",
        }


class TestMateriality:
    """A3-D-3: recipient/amount/commitment → material (re-confirm); phrasing → immaterial."""

    ORIGINAL = {"to": "bob@example.com", "amount": 1500, "body": "please find attached"}

    def test_no_change_is_immaterial(self) -> None:
        assert classify_modification(self.ORIGINAL, dict(self.ORIGINAL)) is Materiality.IMMATERIAL

    def test_phrasing_only_edit_is_immaterial(self) -> None:
        edited = {**self.ORIGINAL, "body": "please see the attached document"}
        assert classify_modification(self.ORIGINAL, edited) is Materiality.IMMATERIAL

    def test_recipient_change_is_material(self) -> None:
        edited = {**self.ORIGINAL, "to": "alice@example.com"}
        assert classify_modification(self.ORIGINAL, edited) is Materiality.MATERIAL

    def test_amount_change_is_material(self) -> None:
        edited = {**self.ORIGINAL, "amount": 2000}
        assert classify_modification(self.ORIGINAL, edited) is Materiality.MATERIAL

    def test_mixed_phrasing_and_material_is_material(self) -> None:
        edited = {**self.ORIGINAL, "body": "new wording", "amount": 2000}
        assert classify_modification(self.ORIGINAL, edited) is Materiality.MATERIAL

    def test_unknown_key_change_defaults_to_material(self) -> None:
        # A key we don't recognise as phrasing is treated as material — the safe direction.
        edited = {**self.ORIGINAL, "weird_field": "x"}
        assert classify_modification(self.ORIGINAL, edited) is Materiality.MATERIAL

    def test_removing_a_material_key_is_material(self) -> None:
        edited = {k: v for k, v in self.ORIGINAL.items() if k != "to"}
        assert classify_modification(self.ORIGINAL, edited) is Materiality.MATERIAL

    def test_removing_a_phrasing_key_is_immaterial(self) -> None:
        edited = {k: v for k, v in self.ORIGINAL.items() if k != "body"}
        assert classify_modification(self.ORIGINAL, edited) is Materiality.IMMATERIAL


# --- the executable admission predicate (the one door onto execution) -----------------------


def test_executable_admits_both_green_lights() -> None:
    """ "The user said yes" is two statuses, and both of them run the action."""
    assert ProposalStatus.executable() == {ProposalStatus.APPROVED, ProposalStatus.MODIFIED}


def test_executable_excludes_everything_that_is_not_a_yes() -> None:
    """Pending, denied, expired and the consumed terminal are not green lights."""
    for status in (
        ProposalStatus.PENDING,
        ProposalStatus.DENIED,
        ProposalStatus.EXPIRED,
        ProposalStatus.CONSUMED,
    ):
        assert status not in ProposalStatus.executable()
