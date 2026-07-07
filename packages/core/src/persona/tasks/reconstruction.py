"""Context reconstruction ordering — the fixed per-leg bootstrap (Spec A2, T3; D-A2-3).

Every leg rebuilds its context in one fixed order:

    contract → checkpoint → last-N leg summaries → live retrieval → the trigger → recite

The **contract leads** so every leg re-anchors on what was actually agreed before anything
can bias it (the anti-drift discipline at the high-attention head); the **next-step is
recited last** (the high-attention tail — the anti-ossification recitation from Manus). The
checkpoint carries yesterday's conclusions; live retrieval brings today's knowledge — so a
leg holds the two against each other (the amnesia/ossification thread).

This module is the *ordering* — pure, in the harness, not the stored state (Anthropic:
"context management belongs in the harness, not the session"). It assembles already-fetched
pieces (the runtime does the actual K3/memory retrieval + pointer dereference, then calls
this); the order is the architectural lock the tests pin exactly. Optional sections are
*omitted* when empty, never reordered.

See ``docs/specs/phase3/spec_A2/decisions.md`` (D-A2-3) and ``docs/research/spec_A2.md`` §1.4.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from persona.tasks.trigger import EventFire, EventTrigger, ScheduledFire, UserReply

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.tasks.checkpoint import TaskCheckpoint
    from persona.tasks.contract import Contract

__all__ = [
    "ReconstructionBlock",
    "ReconstructionStage",
    "RecentLegSummary",
    "reconstruct_context",
]


class ReconstructionStage(StrEnum):
    """The fixed reconstruction stages, in canonical order (D-A2-3)."""

    CONTRACT = "contract"
    CHECKPOINT = "checkpoint"
    RECENT_LEGS = "recent_legs"
    RETRIEVAL = "retrieval"
    TRIGGER = "trigger"
    RECITE_NEXT_STEP = "recite_next_step"


class RecentLegSummary(BaseModel):
    """A tight summary of a recent leg — the last-N continuity window (D-A2-3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    leg_id: str
    summary: str
    outcome: str


class ReconstructionBlock(BaseModel):
    """One ordered section of the reconstructed context (a stage + its rendered text)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: ReconstructionStage
    content: str


def _render_contract(contract: Contract) -> str:
    lines = [f"GOAL: {contract.goal}"]
    if contract.scope:
        lines.append(f"SCOPE: {contract.scope}")
    for criterion in contract.acceptance_criteria:
        lines.append(f"- [{criterion.status.value}] {criterion.id}: {criterion.statement}")
    return "\n".join(lines)


def _render_checkpoint(checkpoint: TaskCheckpoint) -> str:
    lines: list[str] = []
    if checkpoint.progress_conclusions:
        lines.append("CONCLUSIONS:")
        lines.extend(f"- {c}" for c in checkpoint.progress_conclusions)
    if checkpoint.decisions:
        lines.append("DECISIONS:")
        lines.extend(f"- {d.decision} (because {d.rationale})" for d in checkpoint.decisions)
    if checkpoint.lessons:
        lines.append("LESSONS:")
        lines.extend(f"- {lesson}" for lesson in checkpoint.lessons)
    if checkpoint.current_plan:
        lines.append("PLAN:")
        lines.extend(f"- {step}" for step in checkpoint.current_plan)
    if checkpoint.open_questions:
        lines.append("OPEN QUESTIONS:")
        lines.extend(f"- {q}" for q in checkpoint.open_questions)
    if checkpoint.artifact_pointers:
        lines.append("ARTIFACTS:")
        lines.extend(f"- {p.kind}: {p.ref}" for p in checkpoint.artifact_pointers)
    return "\n".join(lines)


def _render_recent_legs(recent_legs: Sequence[RecentLegSummary]) -> str:
    return "\n".join(f"- {s.leg_id} ({s.outcome}): {s.summary}" for s in recent_legs)


def _render_retrieval(retrieval: Sequence[str]) -> str:
    return "\n".join(f"- {snippet}" for snippet in retrieval)


def _render_trigger(trigger: ScheduledFire | UserReply | EventTrigger | EventFire) -> str:
    if isinstance(trigger, ScheduledFire):
        return (
            f"TRIGGER: scheduled fire (schedule {trigger.schedule_id}) "
            f"at {trigger.fire_time.isoformat()}"
        )
    if isinstance(trigger, UserReply):
        return f"TRIGGER: the user replied: {trigger.reply}"
    if isinstance(trigger, EventFire):  # A7's on-event fire — the legible "ran because" (A7-D-6)
        return f"TRIGGER: event ({trigger.event_kind}) — {trigger.human}"
    return f"TRIGGER: event from {trigger.source}: {trigger.payload}"


def reconstruct_context(
    *,
    contract: Contract,
    trigger: ScheduledFire | UserReply | EventTrigger | EventFire,
    checkpoint: TaskCheckpoint | None = None,
    recent_legs: Sequence[RecentLegSummary] = (),
    retrieval: Sequence[str] = (),
) -> tuple[ReconstructionBlock, ...]:
    """Assemble a leg's context in the fixed D-A2-3 order.

    The contract always leads; the trigger always appears; the next-step (if any) is recited
    last. Optional sections (checkpoint, recent legs, retrieval) are omitted when absent —
    never reordered.

    Args:
        contract: The A4-authored anchor (re-read every leg).
        trigger: What woke this leg (the fire / reply / event).
        checkpoint: The latest checkpoint, or ``None`` on the first leg.
        recent_legs: The last-N leg summaries (already bounded by the caller).
        retrieval: Live K3/memory snippets the runtime fetched for this step.

    Returns:
        The ordered reconstruction blocks the leg renders into the loop's input.
    """
    blocks: list[ReconstructionBlock] = [
        ReconstructionBlock(stage=ReconstructionStage.CONTRACT, content=_render_contract(contract))
    ]
    if checkpoint is not None:
        blocks.append(
            ReconstructionBlock(
                stage=ReconstructionStage.CHECKPOINT, content=_render_checkpoint(checkpoint)
            )
        )
    if recent_legs:
        blocks.append(
            ReconstructionBlock(
                stage=ReconstructionStage.RECENT_LEGS, content=_render_recent_legs(recent_legs)
            )
        )
    if retrieval:
        blocks.append(
            ReconstructionBlock(
                stage=ReconstructionStage.RETRIEVAL, content=_render_retrieval(retrieval)
            )
        )
    blocks.append(
        ReconstructionBlock(stage=ReconstructionStage.TRIGGER, content=_render_trigger(trigger))
    )
    if checkpoint is not None and checkpoint.next_step:
        blocks.append(
            ReconstructionBlock(
                stage=ReconstructionStage.RECITE_NEXT_STEP,
                content=f"NEXT STEP: {checkpoint.next_step}",
            )
        )
    return tuple(blocks)
