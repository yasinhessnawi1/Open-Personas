"""The initiative candidate contract (Spec A5, T1; A5-D-2).

persona-core owns the pipeline's entry contract: a frozen
:class:`InitiativeCandidate` carrying the observation, its grounding citations
(min one — no grounding, no candidate), the closed noticing-shape
(:class:`InitiativeTrigger`, A5-D-X-trigger-catalogue: free-form "interesting
observations" are structurally inexpressible), the why-now, the action plan with
its :class:`~persona.tools.categories.ActionCategory` footprint, a required
concrete next step (no actionability ⇒ no candidate), the value estimate, and the
acceptance estimate (a SUPPRESSOR, never a booster — A5-D-X-paccept-suppressor;
the pipeline reads it only after the anti-engagement checks).

The ``source`` tag is the A7 seam: event-sourced candidates enter the SAME
pipeline with ``CandidateSource.EVENT`` and pass the same gates — never a
separate path (spec §2). No DB, no I/O, no LLM here; the scan (runtime) produces
these, the pipeline gates them, the api stores dispositions.

See ``docs/decisions/spec_A5.md`` (A5-D-2 schema, A5-D-4 opportunity key,
A5-D-X-trigger-catalogue) and ``docs/research/spec_A5.md`` §3/§6.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from persona.tools import ActionCategory  # noqa: TC001 — Pydantic needs runtime access

__all__ = [
    "INITIATIVE_TRIGGER_SET_VERSION",
    "CandidateSource",
    "CitationKind",
    "GroundingCitation",
    "InitiativeCandidate",
    "InitiativeTrigger",
    "PlannedStep",
    "ScheduleChange",
    "Urgency",
    "binds_across_triggers",
    "opportunity_anchor",
]


def opportunity_anchor(opportunity_key: str) -> str:
    """The anchor an opportunity key names: everything after the trigger.

    A key is ``{trigger}:{kind}/{ref}`` (:attr:`InitiativeCandidate.opportunity_key`);
    the trigger holds no colon, the ref may (``own_x::node::n1``), so the split is at
    the FIRST colon. A key with no colon is all anchor. A decline and the one live
    notice per opportunity bind this, not the whole key (owner ruling 2026-10-30): the
    same note raised again under another trigger is the same thing to the user.
    """
    return opportunity_key.split(":", 1)[1] if ":" in opportunity_key else opportunity_key


def binds_across_triggers(opportunity_key: str) -> bool:
    """Whether a decline and the live slot bind this key's anchor under every trigger.

    Only a NODE anchor does (the scan's B4 case: one note raised again under another
    trigger). A conversation or task anchor keeps the exact key, as before the ruling,
    so A7's event candidates behave as they did (coordinator ruling 2026-10-30).
    """
    return opportunity_anchor(opportunity_key).startswith(f"{CitationKind.NODE.value}/")


#: Bumped when the trigger catalogue changes (a versioned contract, the
#: WELLBEING_CATEGORY_SET_VERSION discipline). A7 extends the enum ADDITIVELY.
INITIATIVE_TRIGGER_SET_VERSION = "v1"


class InitiativeTrigger(StrEnum):
    """The closed catalogue of noticing shapes (A5-D-X-trigger-catalogue).

    A candidate must carry exactly one of these; open-ended noticing is where
    invention lives (research §6), so anything else fails validation before any
    gate runs. A7's event-sourced triggers extend this additively.
    """

    APPROACHING_COMMITMENT = "approaching_commitment"
    TASK_FOLLOWUP = "task_followup"
    CONFLICT = "conflict"
    STALE_OPEN_LOOP = "stale_open_loop"


class CandidateSource(StrEnum):
    """Where a candidate entered the pipeline (the A7 seam).

    ``SCAN`` is v1's only producer; ``EVENT`` is reserved for A7's event
    triggers, which enqueue into the SAME pipeline — same grounding, envelope,
    restraint, and K4 gates, never a bypass (spec §2).
    """

    SCAN = "scan"
    EVENT = "event"


class Urgency(StrEnum):
    """Whether a surfaced initiative may interrupt or must batch (A5-D-3).

    ``INTERRUPT`` is reserved for hard commitments inside the configured
    horizon, and even then quiet hours are ABSOLUTE (the Phase-3 polarity pin:
    the user's boundary outranks the notice). Everything else batches to the
    morning flush.
    """

    INTERRUPT = "interrupt"
    BATCH = "batch"


class CitationKind(StrEnum):
    """The typed grounding-reference kinds a candidate may cite (A5-D-2)."""

    NODE = "node"
    CONVERSATION = "conversation"
    TASK = "task"


class GroundingCitation(BaseModel):
    """One resolvable grounding reference (A5-D-2).

    ``ref`` is the id in the cited store (graph node id / conversation id /
    task id). Resolution is mechanical: a citation that does not dereference
    through the real read surface discards the candidate — no grounding, no
    candidate (spec criterion 2).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CitationKind
    ref: str = Field(min_length=1)

    @property
    def anchor(self) -> str:
        """The canonical ``kind/ref`` form used in opportunity keys."""
        return f"{self.kind.value}/{self.ref}"


class ScheduleChange(BaseModel):
    """A structured schedule-change action (T9; A5-D-X-delivery-and-doors).

    The CLOSED form of "this initiative proposes retiming an existing task":
    a target task + the proposed cadence (an RFC-5545 RRULE string or a one-time
    instant — the A1 vocabulary, validated at the delivery boundary through the
    parse-honesty rule). When present on a candidate, the PROPOSE delivery routes
    through A8's ``propose_reschedule`` door verbatim — initiative never retimes
    anything directly (spec §2). INERT-UNTIL in v1: no scan emits it (state.md);
    A7's event candidates are the intended producer.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    recurrence_rrule: str | None = None
    one_time_at: datetime | None = None

    @model_validator(mode="after")
    def _exactly_one_cadence(self) -> ScheduleChange:
        if (self.recurrence_rrule is None) == (self.one_time_at is None):
            msg = "a schedule change carries exactly one of recurrence_rrule / one_time_at"
            raise ValueError(msg)
        return self

    @field_validator("one_time_at")
    @classmethod
    def _one_time_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            msg = "naive datetime not allowed; attach a tzinfo"
            raise ValueError(msg)
        return value.astimezone(UTC) if value is not None else None


class PlannedStep(BaseModel):
    """One step of a candidate's proposed action plan, with its category footprint.

    The categories are the A3 risk vocabulary; the envelope decision (T2) reads
    the union across steps: all-safe ⇒ act-then-report, any-gated ⇒ propose-first
    (spec §2). An empty category set is invalid — a step with no resolvable
    effect must not run unattended for free (A3-D-X-completeness's posture).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = Field(min_length=1)
    categories: frozenset[ActionCategory] = Field(min_length=1)


class InitiativeCandidate(BaseModel):
    """A grounded, source-tagged initiative candidate — the pipeline entry contract.

    Attributes:
        observation: What was noticed, in plain terms (the human-readable why's
            substance; K3's honest-attribution rule renders from this + citations).
        citations: Min one resolvable grounding reference. The FIRST citation is
            the anchor — the primary grounding ref that keys the opportunity
            (A5-D-4); the scan orders citations anchor-first by contract.
        trigger: The closed noticing shape (A5-D-X-trigger-catalogue).
        why_now: What changed since the last scan that makes this timely (a fire
            condition, research §6; also what flush-expiry re-checks). USER-FACING
            (R9-256): the one sentence the proposal opens with, addressed to the user
            as "you", holding anything that could have changed lightly. The
            ``observation`` is the grounding statement the judge checks and is not
            shown.
        plan: The proposed steps with their category footprints.
        next_step: The concrete, user-visible next action (actionability is a
            fire condition: no pure-FYI candidates). USER-FACING: it follows "Want
            me to" in the proposal, so it addresses the user as "you".
        value: The scan's value-to-the-user estimate in [0, 1]; gated against
            the configured threshold (below ⇒ silently not raised).
        acceptance: The would-they-welcome-it-now estimate in [0, 1]. A
            SUPPRESSOR only (A5-D-X-paccept-suppressor): it can only gate a
            candidate DOWN and is read only after the anti-engagement checks.
        urgency: Interrupt-vs-batch (A5-D-3's line; quiet hours absolute).
        source: The producing seam (``SCAN`` now, ``EVENT`` = A7).
        owner_id: The user whose graph/tasks ground this candidate.
        persona_id: The persona whose scan produced it (arbitration may hand the
            voicing to a different persona — A5-D-6 — before delivery).
        prompt_version: The scan-prompt artifact version that produced it.
        scanned_at: The schedule fire instant (tz-aware UTC) this scan ran for.
        grounding_fingerprint: The fingerprint of the excerpts (and the judge's
            prompt version) the grounding judge admitted this candidate on (set by
            the pipeline at claim time; ``None`` before).
        grounding_served_model: The chain member that gave that admission, as
            ``"provider/model"``, read from the chain's attempt ledger (set with the
            fingerprint; ``None`` when unclear). At the flush the admitted verdict
            stands, and only the mechanical checks re-run, when the fingerprint
            still matches, this is the head of the judge's chain, and the message
            names no time (each keep is audited as ``initiative.grounding_kept``);
            otherwise the judge is asked again. Rollback hazard (``extra="forbid"``): a build
            that predates these two fields cannot read a notice that carries them;
            see the A5 state's standing bars for the strip before a rollback.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation: str = Field(min_length=1)
    citations: tuple[GroundingCitation, ...] = Field(min_length=1)
    trigger: InitiativeTrigger
    why_now: str = Field(min_length=1)
    plan: tuple[PlannedStep, ...] = Field(min_length=1)
    next_step: str = Field(min_length=1)
    #: The structured schedule-change action (T9): present ⇒ the PROPOSE delivery
    #: routes through A8's door; absent for every ordinary candidate. INERT-UNTIL
    #: in v1 — no scan emits it (A7/test-injected only; see state.md).
    schedule_change: ScheduleChange | None = None
    value: float = Field(ge=0.0, le=1.0)
    acceptance: float = Field(ge=0.0, le=1.0)
    urgency: Urgency
    source: CandidateSource
    owner_id: str = Field(min_length=1)
    persona_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    scanned_at: datetime
    grounding_fingerprint: str | None = None
    grounding_served_model: str | None = None

    @field_validator("scanned_at")
    @classmethod
    def _scanned_at_utc(cls, value: datetime) -> datetime:
        """Reject naive datetimes; normalise tz-aware ones to UTC (the house rule)."""
        if value.tzinfo is None:
            msg = "naive datetime not allowed; use datetime.now(UTC) or attach a tzinfo"
            raise ValueError(msg)
        return value.astimezone(UTC)

    @property
    def anchor(self) -> GroundingCitation:
        """The primary grounding citation (first by contract) keying the opportunity."""
        return self.citations[0]

    @property
    def opportunity_key(self) -> str:
        """The user-level opportunity identity (A5-D-4/D-6).

        ``{trigger}:{kind}/{ref}`` of the anchor citation. Deterministic in the
        candidate's content, so every persona's scan of the same opportunity
        derives the same key — the ledger's ``UNIQUE(owner_id, opportunity_key)``
        then guarantees one user-level notice per opportunity, and a decline of
        this key binds all personas. A decline and the live slot also bind the
        anchor under any other trigger (:func:`opportunity_anchor`).
        """
        return f"{self.trigger.value}:{self.anchor.anchor}"

    @property
    def category_footprint(self) -> frozenset[ActionCategory]:
        """The union of the plan's category footprints (the envelope's input)."""
        return frozenset().union(*(step.categories for step in self.plan))
