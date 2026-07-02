"""Unit tests for the contract draft + build_contract assembler (Spec A4, T1)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.schedules import RecurrenceFreq, RecurrenceRule
from persona.tasks import AcceptanceStatus, UpdateGranularity, UpdatePreference
from persona.tools.categories import ActionCategory
from persona.tools.category_policy import CategoryDecision
from persona_runtime.task_origination import (
    ContractDraft,
    GrantSpec,
    ParsedSchedule,
    build_contract,
)
from pydantic import ValidationError


def _daily_7() -> ParsedSchedule:
    return ParsedSchedule(
        recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY, byhour=(7,)),
        timezone="Europe/Oslo",
        human_terms="every day at 07:00 your time",
    )


def test_build_contract_maps_goal_scope_and_assigns_criterion_ids() -> None:
    draft = ContractDraft(
        goal="track Oslo→Bergen fares",
        scope="under 2000kr",
        acceptance_criteria=("a fare under 2000kr is found", "the user is notified"),
        schedule=_daily_7(),
    )
    contract = build_contract(draft)
    assert contract.goal == "track Oslo→Bergen fares"
    assert contract.scope == "under 2000kr"
    assert [c.id for c in contract.acceptance_criteria] == ["ac1", "ac2"]
    assert contract.acceptance_criteria[0].statement == "a fare under 2000kr is found"
    assert contract.acceptance_criteria[0].status is AcceptanceStatus.PENDING


def test_build_contract_translates_spend_grant_to_policy_and_cap() -> None:
    draft = ContractDraft(
        goal="book the trip",
        grants=(
            GrantSpec(
                category=ActionCategory.SPEND,
                cap_micros=15_000_000,  # 1500 kr
                human="I may book it if it's under 1500kr",
            ),
        ),
    )
    contract = build_contract(draft)
    assert contract.category_policy.decide(ActionCategory.SPEND) is CategoryDecision.ALLOW
    assert contract.bounds.total_budget_micros == 15_000_000


def test_build_contract_translates_deny_grant() -> None:
    draft = ContractDraft(
        goal="research only",
        grants=(
            GrantSpec(category=ActionCategory.EXTERNAL_MUTATE, decision=CategoryDecision.DENY),
        ),
    )
    contract = build_contract(draft)
    assert contract.category_policy.decide(ActionCategory.EXTERNAL_MUTATE) is CategoryDecision.DENY


def test_build_contract_passes_through_update_preference() -> None:
    draft = ContractDraft(
        goal="g",
        updates=UpdatePreference(granularity=UpdateGranularity.QUIET, channel="email"),
    )
    contract = build_contract(draft)
    assert contract.updates is not None
    assert contract.updates.granularity is UpdateGranularity.QUIET
    assert contract.updates.channel == "email"


def test_build_contract_default_has_conservative_policy_and_no_cap() -> None:
    contract = build_contract(ContractDraft(goal="g"))
    # No grants → default policy (gated-by-default categories still gate), no spend cap.
    assert contract.category_policy.decide(ActionCategory.SPEND) is CategoryDecision.GATE
    assert contract.bounds.total_budget_micros is None
    assert contract.updates == UpdatePreference()


def test_grant_cap_only_valid_on_spend() -> None:
    with pytest.raises(ValidationError):
        GrantSpec(category=ActionCategory.EXTERNAL_MUTATE, cap_micros=1000)


def test_parsed_schedule_requires_exactly_one_kind() -> None:
    with pytest.raises(ValidationError):
        ParsedSchedule(timezone="Europe/Oslo", human_terms="x")  # neither
    with pytest.raises(ValidationError):
        ParsedSchedule(
            recurrence=RecurrenceRule(freq=RecurrenceFreq.DAILY),
            one_time_at=datetime(2026, 7, 1, 9, tzinfo=UTC),
            timezone="Europe/Oslo",
            human_terms="x",
        )  # both


def test_parsed_schedule_one_time_must_be_tz_aware() -> None:
    with pytest.raises(ValidationError):
        ParsedSchedule(
            one_time_at=datetime(2026, 7, 1, 9),  # noqa: DTZ001 — naive on purpose
            timezone="Europe/Oslo",
            human_terms="x",
        )


def test_contract_draft_is_frozen() -> None:
    draft = ContractDraft(goal="g")
    with pytest.raises(ValidationError):
        draft.goal = "h"  # type: ignore[misc]
