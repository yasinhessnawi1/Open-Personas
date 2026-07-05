"""Unit tests — the initiative candidate contract (Spec A5, T1; A5-D-2/D-4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from persona.initiative import (
    INITIATIVE_TRIGGER_SET_VERSION,
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.tools.categories import ActionCategory
from pydantic import ValidationError

_SCANNED_AT = datetime(2026, 7, 4, 6, 0, tzinfo=UTC)


def _citation(
    kind: CitationKind = CitationKind.NODE, ref: str = "node-hearing-1"
) -> GroundingCitation:
    return GroundingCitation(kind=kind, ref=ref)


def _step(
    *categories: ActionCategory, description: str = "draft the response letter"
) -> PlannedStep:
    return PlannedStep(
        description=description, categories=frozenset(categories or (ActionCategory.DRAFT,))
    )


def _candidate(**overrides: Any) -> InitiativeCandidate:  # noqa: ANN401 — test fixture override bag
    fields: dict[str, Any] = {
        "observation": "The hearing is on Friday and no response letter exists yet.",
        "citations": (_citation(), _citation(CitationKind.CONVERSATION, "conv-9")),
        "trigger": InitiativeTrigger.APPROACHING_COMMITMENT,
        "why_now": "The hearing date entered the 48h window since the last scan.",
        "plan": (_step(ActionCategory.OBSERVE), _step(ActionCategory.DRAFT)),
        "next_step": "Draft the response letter and present it for review.",
        "value": 0.9,
        "acceptance": 0.8,
        "urgency": Urgency.INTERRUPT,
        "source": CandidateSource.SCAN,
        "owner_id": "user-1",
        "persona_id": "persona-astrid",
        "prompt_version": "v1",
        "scanned_at": _SCANNED_AT,
    }
    fields.update(overrides)
    return InitiativeCandidate(**fields)


class TestGroundingCitation:
    def test_anchor_renders_kind_slash_ref(self) -> None:
        assert _citation().anchor == "node/node-hearing-1"

    def test_empty_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GroundingCitation(kind=CitationKind.NODE, ref="")

    def test_frozen_and_forbid(self) -> None:
        citation = _citation()
        with pytest.raises(ValidationError):
            citation.ref = "other"  # type: ignore[misc]
        with pytest.raises(ValidationError):
            GroundingCitation(kind=CitationKind.NODE, ref="x", extra_field="nope")  # type: ignore[call-arg]


class TestPlannedStep:
    def test_empty_category_set_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PlannedStep(description="do a thing", categories=frozenset())

    def test_frozen(self) -> None:
        step = _step()
        with pytest.raises(ValidationError):
            step.description = "other"  # type: ignore[misc]


class TestInitiativeCandidate:
    def test_valid_candidate_round_trips_json(self) -> None:
        candidate = _candidate()
        restored = InitiativeCandidate.model_validate_json(candidate.model_dump_json())
        assert restored == candidate

    def test_no_citations_no_candidate(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(citations=())

    def test_empty_plan_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(plan=())

    def test_missing_next_step_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(next_step="")

    def test_free_form_trigger_is_inexpressible(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(trigger="interesting_observation")

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(bonus_field="nope")

    @pytest.mark.parametrize("field", ["value", "acceptance"])
    @pytest.mark.parametrize("bad", [-0.1, 1.1])
    def test_estimates_bounded_to_unit_interval(self, field: str, bad: float) -> None:
        with pytest.raises(ValidationError):
            _candidate(**{field: bad})

    def test_naive_scanned_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(scanned_at=datetime(2026, 7, 4, 6, 0))  # noqa: DTZ001 — the rejection under test

    def test_scanned_at_normalised_to_utc(self) -> None:
        oslo_like = timezone(timedelta(hours=2))
        candidate = _candidate(scanned_at=datetime(2026, 7, 4, 8, 0, tzinfo=oslo_like))
        assert candidate.scanned_at == _SCANNED_AT

    def test_event_source_constructs_today(self) -> None:
        """The A7 seam: an event-sourced candidate is expressible now, no scan fields."""
        candidate = _candidate(source=CandidateSource.EVENT)
        assert candidate.source is CandidateSource.EVENT

    def test_anchor_is_first_citation(self) -> None:
        candidate = _candidate()
        assert candidate.anchor == candidate.citations[0]

    def test_opportunity_key_is_trigger_plus_anchor(self) -> None:
        assert _candidate().opportunity_key == "approaching_commitment:node/node-hearing-1"

    def test_opportunity_key_deterministic_across_personas(self) -> None:
        """Two personas scanning the same opportunity derive the SAME key (A5-D-6)."""
        a = _candidate(persona_id="persona-astrid")
        b = _candidate(persona_id="persona-bjorn", observation="Hearing Friday; letter missing.")
        assert a.opportunity_key == b.opportunity_key

    def test_new_anchor_is_new_opportunity(self) -> None:
        """A materially new anchor (a NEW deadline node) is not the declined topic (A5-D-4)."""
        old = _candidate()
        new = _candidate(citations=(_citation(ref="node-hearing-2"),))
        assert old.opportunity_key != new.opportunity_key

    def test_category_footprint_is_union_across_steps(self) -> None:
        candidate = _candidate(
            plan=(
                _step(ActionCategory.OBSERVE),
                _step(ActionCategory.DRAFT, ActionCategory.NOTIFY_USER),
            )
        )
        assert candidate.category_footprint == frozenset(
            {ActionCategory.OBSERVE, ActionCategory.DRAFT, ActionCategory.NOTIFY_USER}
        )


class TestTriggerCatalogue:
    def test_v1_set_is_exactly_the_four_shapes(self) -> None:
        """The closed catalogue (A5-D-X-trigger-catalogue): any expansion is deliberate."""
        assert {t.value for t in InitiativeTrigger} == {
            "approaching_commitment",
            "task_followup",
            "conflict",
            "stale_open_loop",
        }
        assert INITIATIVE_TRIGGER_SET_VERSION == "v1"
