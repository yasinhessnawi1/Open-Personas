"""The gate that decides whether a criterion may advance (R9-164).

This is the safety story for acceptance criteria, so most of what is pinned here is what the
gate REFUSES. The model that reads a leg is the same one that did the work; it proposes, and
nothing it can write reaches the contract except through these rules.
"""

from __future__ import annotations

import pytest
from persona.errors import TaskStateError
from persona.tasks import (
    AcceptanceCriterion,
    AcceptanceStatus,
    Contract,
    CriterionClaim,
    LegEvidence,
    settle_criteria,
)

_CONTRACT = Contract(
    goal="compare three rental deposit schemes",
    acceptance_criteria=(
        AcceptanceCriterion(id="c1", statement="at least three schemes compared"),
        AcceptanceCriterion(id="c2", statement="the comparison is written to a file"),
    ),
)
_EVIDENCE = LegEvidence(
    artifacts=("reports/deposits.md",), sources=("https://lovdata.no/husleieloven",)
)


def _claim(
    criterion_id: str, status: AcceptanceStatus = AcceptanceStatus.DONE, evidence: str = ""
) -> CriterionClaim:
    return CriterionClaim(criterion_id=criterion_id, status=status, evidence=evidence)


def _status(criteria: tuple[AcceptanceCriterion, ...], criterion_id: str) -> AcceptanceStatus:
    return next(c.status for c in criteria if c.id == criterion_id)


def test_a_claim_citing_a_file_this_leg_wrote_lands() -> None:
    criteria, rejected = settle_criteria(
        _CONTRACT, [_claim("c2", evidence="wrote reports/deposits.md")], _EVIDENCE
    )
    assert _status(criteria, "c2") is AcceptanceStatus.DONE
    assert _status(criteria, "c1") is AcceptanceStatus.PENDING  # untouched
    assert rejected == ()


def test_a_claim_citing_a_source_this_leg_read_lands() -> None:
    criteria, _ = settle_criteria(
        _CONTRACT,
        [_claim("c1", evidence="compared all three against https://lovdata.no/husleieloven")],
        _EVIDENCE,
    )
    assert _status(criteria, "c1") is AcceptanceStatus.DONE


def test_a_citation_survives_reformatting() -> None:
    """A model that writes the path with different case or spacing still points at it."""
    criteria, _ = settle_criteria(
        _CONTRACT, [_claim("c2", evidence="Wrote   Reports/Deposits.MD   as agreed")], _EVIDENCE
    )
    assert _status(criteria, "c2") is AcceptanceStatus.DONE


# --- what the gate refuses ---------------------------------------------------


def test_an_unevidenced_claim_is_refused() -> None:
    """The cheap failure: a model marking the checklist done because the leg ended."""
    criteria, rejected = settle_criteria(
        _CONTRACT, [_claim("c1", evidence="I completed this")], _EVIDENCE
    )
    assert _status(criteria, "c1") is AcceptanceStatus.PENDING
    assert "names no file" in rejected[0].reason


def test_a_claim_from_a_leg_that_produced_nothing_is_refused() -> None:
    criteria, rejected = settle_criteria(
        _CONTRACT, [_claim("c1", evidence="reports/deposits.md")], LegEvidence()
    )
    assert _status(criteria, "c1") is AcceptanceStatus.PENDING
    assert rejected[0].criterion_id == "c1"


def test_a_claim_from_an_errored_leg_is_refused() -> None:
    criteria, rejected = settle_criteria(
        _CONTRACT,
        [_claim("c2", evidence="wrote reports/deposits.md")],
        _EVIDENCE.model_copy(update={"errored": True}),
    )
    assert _status(criteria, "c2") is AcceptanceStatus.PENDING
    assert "errored" in rejected[0].reason


def test_an_invented_criterion_id_changes_nothing() -> None:
    criteria, rejected = settle_criteria(
        _CONTRACT, [_claim("c9", evidence="reports/deposits.md")], _EVIDENCE
    )
    assert criteria == _CONTRACT.acceptance_criteria
    assert "no such criterion" in rejected[0].reason


def test_a_settled_criterion_cannot_be_reopened() -> None:
    """Both directions: a later leg can neither un-finish it nor re-finish it."""
    done = _CONTRACT.model_copy(
        update={
            "acceptance_criteria": (
                AcceptanceCriterion(
                    id="c1",
                    statement="at least three schemes compared",
                    status=AcceptanceStatus.DONE,
                ),
            )
        }
    )
    _, rejected = settle_criteria(
        done,
        [
            _claim("c1", AcceptanceStatus.FAILED, evidence="actually only two"),
            _claim("c1", AcceptanceStatus.PENDING, evidence="reports/deposits.md"),
        ],
        _EVIDENCE,
    )
    assert len(rejected) == 2


def test_nothing_can_be_pushed_back_to_pending() -> None:
    _, rejected = settle_criteria(
        _CONTRACT, [_claim("c1", AcceptanceStatus.PENDING, evidence="x")], _EVIDENCE
    )
    assert "contract amendment" in rejected[0].reason


def test_the_same_criterion_twice_in_one_leg_keeps_the_first() -> None:
    criteria, rejected = settle_criteria(
        _CONTRACT,
        [
            _claim("c2", evidence="wrote reports/deposits.md"),
            _claim("c2", AcceptanceStatus.FAILED, evidence="on reflection, no"),
        ],
        _EVIDENCE,
    )
    assert _status(criteria, "c2") is AcceptanceStatus.DONE
    assert "twice" in rejected[0].reason


# --- failure is claimable, and reversible ------------------------------------


def test_a_failed_claim_needs_a_reason_but_no_citation() -> None:
    criteria, _ = settle_criteria(
        _CONTRACT,
        [_claim("c1", AcceptanceStatus.FAILED, evidence="only two schemes exist in Norway")],
        LegEvidence(),
    )
    assert _status(criteria, "c1") is AcceptanceStatus.FAILED


def test_a_failed_claim_with_no_reason_is_refused() -> None:
    _, rejected = settle_criteria(_CONTRACT, [_claim("c1", AcceptanceStatus.FAILED)], _EVIDENCE)
    assert "must say why" in rejected[0].reason


def test_a_later_leg_can_fix_a_failed_criterion() -> None:
    failed = _CONTRACT.model_copy(
        update={
            "acceptance_criteria": (
                AcceptanceCriterion(
                    id="c1",
                    statement="at least three schemes compared",
                    status=AcceptanceStatus.FAILED,
                ),
            )
        }
    )
    criteria, rejected = settle_criteria(
        failed, [_claim("c1", evidence="found the third at reports/deposits.md")], _EVIDENCE
    )
    assert _status(criteria, "c1") is AcceptanceStatus.DONE
    assert rejected == ()


# --- the entity's status-only path -------------------------------------------


def test_the_task_refuses_a_rewrite_wearing_a_status_updates_clothes() -> None:
    from datetime import UTC, datetime

    from persona.tasks import Task

    now = datetime(2026, 9, 14, tzinfo=UTC)
    task = Task(
        id="t1",
        owner_id="u",
        persona_id="p",
        contract=_CONTRACT,
        created_at=now,
        updated_at=now,
    )
    rewritten = (
        AcceptanceCriterion(
            id="c1", statement="at least ONE scheme compared", status=AcceptanceStatus.DONE
        ),
        _CONTRACT.acceptance_criteria[1],
    )
    with pytest.raises(TaskStateError):
        task.settle_criteria(rewritten, now=now)


def test_the_task_advances_a_status_and_nothing_else() -> None:
    from datetime import UTC, datetime

    from persona.tasks import Task

    now = datetime(2026, 9, 14, tzinfo=UTC)
    task = Task(
        id="t1",
        owner_id="u",
        persona_id="p",
        contract=_CONTRACT,
        created_at=now,
        updated_at=now,
    )
    criteria, _ = settle_criteria(
        _CONTRACT, [_claim("c2", evidence="wrote reports/deposits.md")], _EVIDENCE
    )
    settled = task.settle_criteria(criteria, now=now)
    assert _status(settled.contract.acceptance_criteria, "c2") is AcceptanceStatus.DONE
    assert settled.contract.goal == _CONTRACT.goal
    # A no-op settle returns the same task rather than a pointless new version.
    assert settled.settle_criteria(criteria, now=now) is settled
