"""The task contract — the A4-authored anchor against drift (Spec A2, T2; D-A2-1).

Over many legs, small reinterpretations of the goal compound (the classic long-horizon
failure). The contract is the fixed anchor every leg re-reads: a frozen value type with
the goal, scope, acceptance criteria (statement + status), and stated bounds. It carries
**no mutation method** — a leg structurally cannot rewrite it. Acceptance-criterion
*status* advances through the Task (the controlled, status-only path), never by editing
the goal/scope/statements; a task that concludes the contract itself must change goes
``waiting(on_user)`` with A4's amendment proposal (spec §3), it does not improvise.

The cadence the task runs at (the contract's "schedule" aspect) is realised as the
task's ``schedule_id`` linkage to A1's durable schedule entity, not embedded here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from persona.tools.category_policy import (  # noqa: TC001 — Pydantic needs runtime access
    DEFAULT_POLICY,
    CategoryPolicy,
)

__all__ = [
    "AcceptanceCriterion",
    "AcceptanceStatus",
    "Contract",
    "ContractBounds",
    "Deliverable",
    "DeliverableFormat",
    "UpdateGranularity",
    "UpdatePreference",
]


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalise tz-aware ones to UTC (house rule)."""
    if value.tzinfo is None:
        msg = "naive datetime not allowed; use datetime.now(UTC) or attach a tzinfo"
        raise ValueError(msg)
    return value.astimezone(UTC)


class AcceptanceStatus(StrEnum):
    """The pass/fail status of one acceptance criterion (drives pick-highest-not-done)."""

    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class AcceptanceCriterion(BaseModel):
    """One acceptance criterion: an immutable statement + a mutable-via-Task status.

    The ``statement`` and ``id`` are part of the A4-authored anchor (a leg cannot
    change them); the ``status`` advances as work progresses (through the Task).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    statement: str
    status: AcceptanceStatus = AcceptanceStatus.PENDING


class ContractBounds(BaseModel):
    """Stated task-level bounds the contract agreed (A4 authors; A3 enforces).

    All optional — A2 carries the *data*; A3 owns budgets-as-policy. The per-leg box
    (steps/budget/wall-clock) is separate (D-A2-2); these are whole-task limits.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_budget_micros: int | None = None
    deadline: datetime | None = None
    max_legs: int | None = None

    @field_validator("deadline", mode="after")
    @classmethod
    def _deadline_tz_aware(cls, value: datetime | None) -> datetime | None:
        return _ensure_utc(value) if value is not None else None


class DeliverableFormat(StrEnum):
    """The shape a finished task hands back (Spec W1, T11).

    Small and closed on purpose. A task that finishes into "some prose, probably" is a task
    whose output nobody can use twice, and the alternative (a free-text format field the
    model fills) produces a different answer every leg. These four cover what the tasks in
    this product actually produce; adding one is an enum member plus a line of guidance.
    """

    #: The default: findings with their sources, in markdown, headed and skimmable.
    FINDINGS_MARKDOWN = "findings_markdown"
    #: A short written answer, no structure imposed.
    PROSE = "prose"
    #: Rows and columns, when the goal is a comparison or a list.
    TABLE = "table"
    #: A file in the workspace (the filename says which), for anything a person opens.
    FILE = "file"


class Deliverable(BaseModel):
    """What "done" looks like for this task (Spec W1, T11).

    Part of the A4-authored anchor, so it cannot drift leg to leg: the persona agreed a
    shape at the start, and every leg re-reads it. ``filename`` is a convention, not a
    promise of storage: a task that writes a file names it here so the successor leg writes
    to the same place instead of inventing a second one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: DeliverableFormat = DeliverableFormat.FINDINGS_MARKDOWN
    filename: str = ""

    def render(self) -> str:
        """One line naming the agreed shape, for the contract block and the report."""
        if self.filename:
            return f"{self.format.value} in {self.filename}"
        return self.format.value


class UpdateGranularity(StrEnum):
    """How much of a task's progress reaches the user (Spec A4, A4-D-3).

    Aligned with the A2-D-4 episodic milestone kinds
    (``task_started``/``major_progress``/``waiting``/``completed``/``failed``):
    ``MILESTONES`` is the default and emits exactly those. ``QUIET`` suppresses the
    ``progress`` message class only — A3's always-pass classes (approval/failure/safety)
    still reach the user regardless of this preference (the un-suppressible floor).
    """

    EVERY_LEG = "every_leg"
    MILESTONES = "milestones"
    COMPLETION_ONLY = "completion_only"
    QUIET = "quiet"


class UpdatePreference(BaseModel):
    """How the persona reports a task's progress (Spec A4 extension of A2's Contract).

    An additive, optional part of the contract — the persona's stated agreement on *how*
    you will hear about the work ("I'll check in at milestones, on web"). It rides the
    contract's ``contract_json`` blob; no storage column. ``channel`` is a
    ``DeliveryRouter`` registry key (e.g. ``"web"``); ``None`` lets the router fall back to
    the conversation's home channel (A4-D-6).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    granularity: UpdateGranularity = UpdateGranularity.MILESTONES
    channel: str | None = None


class Contract(BaseModel):
    """The frozen, A4-authored anchor: goal, scope, acceptance criteria, bounds (D-A2-1).

    A leg re-reads this verbatim every reconstruction (the anti-drift discipline) and
    can never write it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: str
    scope: str = ""
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    bounds: ContractBounds = ContractBounds()
    # The A3 per-task permission matrix (A4 authors; A3 enforces). Defaults to the
    # conservative seed (free categories allow, gated-by-default categories gate) so an
    # unconfigured task is bounded; rides ``contract_json``, no migration column.
    category_policy: CategoryPolicy = Field(default=DEFAULT_POLICY)
    # A4 extension (A4-D-1): how the persona reports progress. Optional + ``None`` default
    # so every pre-A4 ``contract_json`` blob deserializes byte-compatibly through this
    # frozen ``extra="forbid"`` model; rides ``contract_json``, no migration.
    updates: UpdatePreference | None = None
    # Spec W1 (T11): the agreed shape of the finished work. Defaulted, so every contract
    # written before this field existed keeps validating, and an ad hoc task that nobody
    # specified a format for still knows what to produce: structured findings markdown.
    deliverable: Deliverable = Deliverable()
