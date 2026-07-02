"""Unit tests for the task contract (Spec A2, T2).

The contract is the A4-authored anchor against drift (D-A2-1). It is a frozen value
type: goal, scope, acceptance criteria (statement + status), and stated bounds. It
carries NO mutation method — a leg structurally cannot rewrite it; status advances
through the Task (test_task.py), never on the contract object.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.tasks import (
    AcceptanceCriterion,
    AcceptanceStatus,
    Contract,
    ContractBounds,
    UpdateGranularity,
    UpdatePreference,
)
from pydantic import ValidationError


def test_acceptance_status_values() -> None:
    assert {s.value for s in AcceptanceStatus} == {"pending", "done", "failed"}


def test_acceptance_criterion_defaults_pending() -> None:
    c = AcceptanceCriterion(id="c1", statement="prices tracked in prices.csv")
    assert c.status == AcceptanceStatus.PENDING


def test_acceptance_criterion_is_frozen_and_forbids_extra() -> None:
    c = AcceptanceCriterion(id="c1", statement="x")
    with pytest.raises(ValidationError):
        c.statement = "y"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        AcceptanceCriterion(id="c1", statement="x", extra="no")  # type: ignore[call-arg]


def test_minimal_contract() -> None:
    contract = Contract(goal="find the cheapest Oslo→Bergen fare this week")
    assert contract.goal.startswith("find the cheapest")
    assert contract.scope == ""
    assert contract.acceptance_criteria == ()
    assert contract.bounds == ContractBounds()


def test_full_contract_round_trips() -> None:
    contract = Contract(
        goal="find the cheapest fare",
        scope="self-hosted only; under 2000kr",
        acceptance_criteria=(
            AcceptanceCriterion(id="c1", statement="a fare under 2000kr is found"),
            AcceptanceCriterion(
                id="c2", statement="the user is notified", status=AcceptanceStatus.DONE
            ),
        ),
        bounds=ContractBounds(total_budget_micros=500_000, max_legs=20),
    )
    assert contract.acceptance_criteria[1].status == AcceptanceStatus.DONE
    assert contract.bounds.total_budget_micros == 500_000
    assert contract.bounds.max_legs == 20


def test_contract_is_frozen_and_forbids_extra() -> None:
    contract = Contract(goal="x")
    with pytest.raises(ValidationError):
        contract.goal = "y"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        Contract(goal="x", surprise="no")  # type: ignore[call-arg]


def test_contract_bounds_all_optional() -> None:
    bounds = ContractBounds()
    assert bounds.total_budget_micros is None
    assert bounds.deadline is None
    assert bounds.max_legs is None


def test_contract_bounds_deadline_must_be_tz_aware() -> None:
    with pytest.raises(ValidationError):
        ContractBounds(deadline=datetime(2026, 6, 24, 12, 0))  # noqa: DTZ001 — naive on purpose
    ok = ContractBounds(deadline=datetime(2026, 6, 24, 12, 0, tzinfo=UTC))
    assert ok.deadline is not None


# --- A4 extension: UpdatePreference on the contract (A4-D-1, A4-D-3) ---------------------


def test_update_granularity_values() -> None:
    assert {g.value for g in UpdateGranularity} == {
        "every_leg",
        "milestones",
        "completion_only",
        "quiet",
    }


def test_update_preference_defaults_to_milestones_and_home_channel() -> None:
    pref = UpdatePreference()
    assert pref.granularity is UpdateGranularity.MILESTONES
    assert pref.channel is None


def test_update_preference_is_frozen_and_forbids_extra() -> None:
    pref = UpdatePreference(channel="web")
    with pytest.raises(ValidationError):
        pref.channel = "email"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        UpdatePreference(cadence="no")  # type: ignore[call-arg]


def test_contract_updates_defaults_none() -> None:
    # A minimal (pre-A4) contract has no update preference set.
    assert Contract(goal="x").updates is None


def test_contract_carries_update_preference() -> None:
    contract = Contract(
        goal="watch the portal",
        updates=UpdatePreference(granularity=UpdateGranularity.QUIET, channel="email"),
    )
    assert contract.updates is not None
    assert contract.updates.granularity is UpdateGranularity.QUIET
    assert contract.updates.channel == "email"


def test_pre_a4_contract_json_deserializes_byte_compatibly() -> None:
    # The byte-compatibility guarantee (A4-D-1): a contract_json blob written before A4
    # existed (no ``updates`` key) must still validate through the frozen extra="forbid"
    # model, with ``updates`` defaulting to None.
    legacy_blob = {
        "goal": "find the cheapest fare",
        "scope": "under 2000kr",
        "acceptance_criteria": [{"id": "c1", "statement": "fare found", "status": "pending"}],
        "bounds": {"total_budget_micros": 500_000, "deadline": None, "max_legs": 20},
        "category_policy": {"overrides": []},
    }
    contract = Contract.model_validate(legacy_blob)
    assert contract.updates is None
    assert contract.goal == "find the cheapest fare"


def test_contract_updates_round_trips_through_json() -> None:
    contract = Contract(
        goal="g",
        updates=UpdatePreference(granularity=UpdateGranularity.EVERY_LEG, channel="web"),
    )
    restored = Contract.model_validate(contract.model_dump(mode="json"))
    assert restored == contract
    assert restored.updates is not None
    assert restored.updates.granularity is UpdateGranularity.EVERY_LEG
