"""The in-flight contract draft and its assembly into a frozen A2 contract (Spec A4, T1).

A :class:`ContractDraft` is the conversational artifact the persona builds from a turn
carrying standing intent: the goal/scope in its words, the acceptance criteria, the parsed
cadence (A1-shaped), any beyond-default permissions, and the update preference. It is what
the echo renders (T3), what adjust-by-reply edits (T3), and what — on one explicit
confirmation — the api turns into the A2 task + A1 schedule + A3 matrix (T6).

:func:`build_contract` is the pure mapping from a draft to the frozen
:class:`persona.tasks.Contract` A2 stores: the draft's permissions become the A3
``CategoryPolicy`` overrides + the spend cap on ``ContractBounds`` (A4 authors; A3 enforces).
The cadence is *not* embedded in the contract (it rides ``Task.schedule_id``, A2's design);
the draft carries it for the create step.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access (field annotation)

from persona.schedules import RecurrenceRule  # noqa: TC002 — Pydantic needs runtime access
from persona.tasks import (
    AcceptanceCriterion,
    Contract,
    ContractBounds,
    UpdatePreference,
)
from persona.tools.categories import ActionCategory
from persona.tools.category_policy import (
    CategoryDecision,
    CategoryPolicy,
    CategoryRule,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "ContractDraft",
    "GrantSpec",
    "ParsedSchedule",
    "build_contract",
    "canonicalize_draft",
]


class GrantSpec(BaseModel):
    """One beyond-default permission the conversation granted (Spec A4).

    Every grant beyond the A3 defaults is surfaced as its own *prominent* line in the echo
    (criterion 4 — no surprise grants) and lands as a :class:`CategoryRule` override on the
    contract's policy. A :data:`ActionCategory.SPEND` grant carries its cap in
    ``cap_micros`` (→ ``ContractBounds.total_budget_micros``).

    Attributes:
        category: The action category this grant governs.
        decision: The posture the grant sets (usually ``ALLOW`` to loosen a gated category,
            or ``DENY`` to tighten beyond the default).
        cap_micros: For a ``SPEND`` grant, the spend cap in micros (1 kr = 10_000 micros);
            ``None`` for non-spend grants.
        human: The persona-voiced echo line, stated prominently
            ("I may book it if it's under 1500kr — a spend permission with a 1500kr cap").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: ActionCategory
    decision: CategoryDecision = CategoryDecision.ALLOW
    cap_micros: int | None = Field(default=None, ge=0)
    human: str = ""

    @model_validator(mode="after")
    def _cap_only_on_spend(self) -> GrantSpec:
        if self.cap_micros is not None and self.category is not ActionCategory.SPEND:
            msg = "cap_micros is only meaningful on a SPEND grant"
            raise ValueError(msg)
        return self


class ParsedSchedule(BaseModel):
    """A schedule phrase parsed to an A1-shaped cadence + its human-terms rendering (A4-D-1).

    Exactly one of ``recurrence`` (a recurring rule) or ``one_time_at`` (a single future
    instant) is set — the same XOR A1's :class:`persona.schedules.Schedule` enforces. The
    parser (T2) produces this; an *unrepresentable* phrase is declined honestly, never
    coerced into an approximate rule (parse-honesty, criterion 4).

    Attributes:
        recurrence: The recurring rule, or ``None`` for a one-time schedule.
        one_time_at: The single future UTC instant, or ``None`` for a recurring schedule.
        timezone: The IANA timezone the cadence is anchored in (the localization frame).
        human_terms: The cadence echoed in human terms ("every weekday at 07:00 your time").
        cadence_note: The honest decline rider (R4, BUG A): when the ASKED cadence was
            unrepresentable and the judge fell back to a run-once, this names what could
            not be set and the nearest cadences that CAN be — rendered as its own echo
            line so the fallback is never silent. Empty for a faithfully-parsed cadence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    recurrence: RecurrenceRule | None = None
    one_time_at: datetime | None = None
    timezone: str
    human_terms: str
    cadence_note: str = ""

    @model_validator(mode="after")
    def _exactly_one_kind(self) -> ParsedSchedule:
        has_recurrence = self.recurrence is not None
        has_one_time = self.one_time_at is not None
        if has_recurrence == has_one_time:
            msg = "a ParsedSchedule has exactly one of recurrence or one_time_at"
            raise ValueError(msg)
        if self.one_time_at is not None and self.one_time_at.tzinfo is None:
            msg = "one_time_at must be tz-aware"
            raise ValueError(msg)
        return self


class ContractDraft(BaseModel):
    """The in-flight contract draft assembled from the conversation (Spec A4, T1).

    The unit the echo renders, adjust-by-reply edits, and confirmation creates. Frozen —
    an amendment produces a new draft (``model_copy``), never an in-place mutation, so the
    echo↔draft correspondence is always exact.

    Attributes:
        goal: The task's objective, in the persona's words.
        scope: The scope statement.
        acceptance_criteria: The criterion *statements* (ids are assigned at
            :func:`build_contract` time; the draft holds the human strings).
        schedule: The parsed cadence, or ``None`` for an as-yet-unparsed/declined schedule.
        grants: The beyond-default permissions (each its own prominent echo line).
        updates: How the persona will report progress (digest granularity + channel).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str
    scope: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    schedule: ParsedSchedule | None = None
    grants: tuple[GrantSpec, ...] = ()
    updates: UpdatePreference = UpdatePreference()


def build_contract(draft: ContractDraft) -> Contract:
    """Assemble a frozen :class:`persona.tasks.Contract` from a draft (A4-D-1).

    The draft's permissions become the A3 ``CategoryPolicy`` overrides (A4 authors; A3
    enforces); a ``SPEND`` grant's ``cap_micros`` becomes ``ContractBounds.total_budget_micros``.
    Acceptance-criterion ids are assigned deterministically (``ac1``, ``ac2``, …) so a
    rebuilt draft yields a stable contract. The cadence is intentionally absent — it rides
    ``Task.schedule_id`` (A2's design), not the contract.

    Args:
        draft: The conversational draft to freeze into a contract.

    Returns:
        The A4-authored :class:`Contract` (goal/scope/criteria/bounds/policy/updates).
    """
    criteria = tuple(
        AcceptanceCriterion(id=f"ac{index}", statement=statement)
        for index, statement in enumerate(draft.acceptance_criteria, start=1)
    )
    policy = CategoryPolicy(
        overrides=tuple(
            CategoryRule(category=grant.category, decision=grant.decision) for grant in draft.grants
        )
    )
    spend_cap = next(
        (
            grant.cap_micros
            for grant in draft.grants
            if grant.category is ActionCategory.SPEND and grant.cap_micros is not None
        ),
        None,
    )
    bounds = ContractBounds(total_budget_micros=spend_cap)
    return Contract(
        goal=draft.goal,
        scope=draft.scope,
        acceptance_criteria=criteria,
        bounds=bounds,
        category_policy=policy,
        updates=draft.updates,
    )


def canonicalize_draft(draft: ContractDraft) -> ContractDraft:
    """Return a draft in canonical form — grants ordered deterministically (Spec A4, T4).

    A model-produced draft may emit grants in any order; the downstream confirm→create
    idempotency token (T6) hashes the draft, so two confirmations of the *same* contract
    must serialise identically. Grants are sorted by category (a total, stable order);
    acceptance-criteria order is meaningful (the persona's stated order) and is preserved.
    Idempotent: ``canonicalize_draft(canonicalize_draft(d)) == canonicalize_draft(d)``.
    """
    ordered_grants = tuple(sorted(draft.grants, key=lambda g: g.category.value))
    return draft.model_copy(update={"grants": ordered_grants})
