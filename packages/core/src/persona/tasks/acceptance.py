"""Advancing an acceptance criterion, and the gate that decides whether it may (R9-164).

:class:`~persona.tasks.contract.AcceptanceStatus` has three values and only one of them was
ever reachable. Criteria were authored, persisted, rendered into every leg's contract block as
``[pending]``, and returned to the web, so a task re-read its whole checklist as unfinished
forever and a finished task's completion report listed nothing as done.

The missing half is not "write the status" (three lines) but **who is allowed to say so**. A
criterion is the user's definition of done, and the party doing the work is the last party
that should be trusted to mark its own work finished. So the shape here is deliberately
lopsided:

- A leg's work is read by a model, which may **propose** that a criterion is met and must say
  what it is pointing at.
- This module, which no leg and no prompt can reach, decides whether the proposal LANDS. It
  refuses anything it cannot check: an id the contract does not have, a criterion already
  settled, a leg that errored, and above all a claim whose evidence cites nothing the leg
  actually did.

What that buys and what it does not is worth being exact about, because the gate is the whole
safety story. It proves the leg really produced the file or really read the source it cites.
It does NOT prove the file satisfies the criterion: a model that read three pages can cite one
of them for a criterion it did not finish, and this gate will accept it. What it makes
impossible is the cheap failure, which is a model that marks a checklist done at the end of a
leg that produced nothing at all.

Pure: no I/O, no clock, no model. The runtime's assessor proposes; the api persists through
:meth:`persona.tasks.entity.Task.settle_criteria`, the status-only path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from persona.tasks.contract import AcceptanceCriterion, AcceptanceStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.tasks.contract import Contract

__all__ = [
    "CriterionClaim",
    "LegEvidence",
    "RejectedClaim",
    "settle_criteria",
]


class CriterionClaim(BaseModel):
    """One proposal that a criterion's status should change, with what it points at.

    ``evidence`` is not decoration. It is the only thing the gate can check, so a claim
    without one that names a real file or a real source is refused.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str
    status: AcceptanceStatus
    evidence: str = ""


class LegEvidence(BaseModel):
    """The durable facts about ONE leg that a claim may cite.

    Deliberately this leg's own facts, not the task's accumulated ledgers. A leg that did
    nothing this time cannot advance a criterion on the strength of what some earlier leg
    read, which is exactly the move a checklist-completing model would otherwise make.

    Attributes:
        artifacts: Workspace paths this leg persisted (the Spec-28 channel).
        sources: URLs this leg's tool results actually named.
        errored: Whether the run ended in an error. A leg that blew up finished nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifacts: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    errored: bool = False

    def cites_something_real(self, evidence: str) -> bool:
        """Does this text point at a file this leg wrote or a source this leg read?

        Substring, whitespace- and case-normalised: a model that writes "wrote
        Reports/Week-24.md as agreed" is pointing at ``reports/week-24.md``. Anything that
        names none of them is a sentence about the work rather than a pointer to it.
        """
        haystack = " ".join(evidence.split()).casefold()
        if not haystack:
            return False
        return any(fact.casefold() in haystack for fact in (*self.artifacts, *self.sources) if fact)


class RejectedClaim(BaseModel):
    """A claim the gate refused, and why (logged, never silently dropped)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion_id: str
    reason: str


def settle_criteria(
    contract: Contract, claims: Sequence[CriterionClaim], evidence: LegEvidence
) -> tuple[tuple[AcceptanceCriterion, ...], tuple[RejectedClaim, ...]]:
    """Apply the claims the gate accepts; return the new criteria and the refusals.

    The rules, in the order they are checked:

    1. The criterion must exist on the contract. A model that invents an id changes nothing.
    2. The claimed status must be ``done`` or ``failed``. Nothing may be pushed back to
       ``pending``: un-finishing work is a contract amendment, which is the user's.
    3. A criterion already ``done`` is settled. Only an amendment reopens it, so a later leg
       cannot quietly erase a finished one, and two legs cannot flip it back and forth.
    4. A ``done`` claim needs a leg that did not error AND evidence citing a file this leg
       wrote or a source this leg read.
    5. A ``failed`` claim needs a stated reason but no citation. Saying your own work failed
       is against interest, and the honest signal is worth more than the proof. It is
       reversible: a later leg that fixes it may claim ``done`` with evidence.
    6. One claim per criterion. A batch that names the same criterion twice keeps the first.

    Args:
        contract: The task's frozen contract (read only; the statements never change here).
        claims: What the assessor proposed.
        evidence: What this leg durably did.

    Returns:
        ``(criteria, rejected)`` — the full criteria tuple in contract order (unchanged
        entries included, so the caller can compare it against the contract's own), and every
        refused claim with its reason.
    """
    by_id = {c.id: c for c in contract.acceptance_criteria}
    accepted: dict[str, AcceptanceStatus] = {}
    rejected: list[RejectedClaim] = []

    for claim in claims:
        current = by_id.get(claim.criterion_id)
        reason = _refusal(claim, current, evidence, already_claimed=claim.criterion_id in accepted)
        if reason is not None:
            rejected.append(RejectedClaim(criterion_id=claim.criterion_id, reason=reason))
            continue
        accepted[claim.criterion_id] = claim.status

    criteria = tuple(
        c.model_copy(update={"status": accepted[c.id]}) if c.id in accepted else c
        for c in contract.acceptance_criteria
    )
    return criteria, tuple(rejected)


def _refusal(
    claim: CriterionClaim,
    current: AcceptanceCriterion | None,
    evidence: LegEvidence,
    *,
    already_claimed: bool,
) -> str | None:
    """Why this claim cannot land, or ``None`` if it may."""
    if current is None:
        return "no such criterion on the contract"
    if already_claimed:
        return "the same criterion was claimed twice in one leg"
    if claim.status is AcceptanceStatus.PENDING:
        return "a criterion cannot be pushed back to pending; that is a contract amendment"
    if current.status is AcceptanceStatus.DONE:
        return "already done; reopening a settled criterion is a contract amendment"
    if claim.status is AcceptanceStatus.FAILED:
        return None if claim.evidence.strip() else "a failed criterion must say why"
    if evidence.errored:
        return "the leg errored; nothing it claims to have finished is established"
    if not evidence.cites_something_real(claim.evidence):
        return "the evidence names no file this leg wrote and no source this leg read"
    return None
