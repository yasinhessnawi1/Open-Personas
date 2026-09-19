"""Context reconstruction ordering — the fixed per-leg bootstrap (Spec A2, T3; D-A2-3).

Every leg rebuilds its context in one fixed order:

    contract → method → checkpoint → last-N leg summaries → live retrieval → trigger → recite

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

METHOD (Spec W1, D-W1-12) sits second, right under the contract: how to work, read before
any of the work itself. It is the same six rules every leg, never per-task guidance, and it
is rendered here rather than in the loop's own instructions so it reaches legs only. A chat
one-off shares the loop and does not get it: a person is there, watching, and can redirect.

See ``docs/specs/phase3/spec_A2/decisions.md`` (D-A2-3) and ``docs/research/spec_A2.md`` §1.4.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from persona.tasks.trigger import (
    AutoRetry,
    EventFire,
    EventTrigger,
    Revived,
    ScheduledFire,
    UserDispatch,
    UserReply,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.tasks.checkpoint import TaskCheckpoint
    from persona.tasks.contract import Contract

__all__ = [
    "METHOD_BLOCK",
    "ReconstructionBlock",
    "ReconstructionStage",
    "RecentLegSummary",
    "reconstruct_context",
]

#: How a leg works, rendered every leg right after the contract (Spec W1, D-W1-12).
#:
#: Six rules, and each one is a failure that was actually observed in a leg: re-searching what
#: the last leg concluded, one lookup per step at one model call each, the same failing call
#: retried verbatim, a query already run three legs ago, a finished answer delivered in a
#: shape nobody asked for, and a leg that hit its wall-clock with everything it had learned
#: still in its head. Universal, never per-task: the contract says WHAT, this says HOW.
METHOD_BLOCK = """METHOD:
- Read the conclusions, the queries already run and the recalled memory below before any
  external lookup. If they already answer part of the goal, do not look it up again.
- Batch independent lookups into ONE step. Five searches in one step cost one model call;
  one per step costs five.
- Never repeat a call that failed with the same arguments. Change the query, change the
  tool, or work with what you have.
- The queries and sources listed below are yours from earlier legs. Do not run them again;
  build on what they already found.
- Deliver in the format the contract asks for.
- Write what you have concluded before your time runs out. A leg keeps only what it wrote
  down, so record partial findings as you go rather than at the end."""


class ReconstructionStage(StrEnum):
    """The fixed reconstruction stages, in canonical order (D-A2-3)."""

    CONTRACT = "contract"
    METHOD = "method"
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
    # Spec W1 (T11): the agreed shape of the finished work, in the anchor the leg re-reads,
    # so "done" means the same thing on leg 6 as it did on leg 1.
    lines.append(f"DELIVERABLE: {contract.deliverable.render()}")
    # Issue #16: the files the person attached when they handed the work over. Named in the
    # anchor the leg re-reads, so leg 6 of a task and occurrence 40 of a routine open the
    # same files leg 1 did. The paths are workspace-relative, which is exactly what the
    # file_read tool takes, so this is a pointer and not a copy of the bytes.
    if contract.attachments:
        lines.append("ATTACHED FILES (read them with file_read before you start):")
        lines.extend(f"- {attachment.render()}" for attachment in contract.attachments)
    for criterion in contract.acceptance_criteria:
        lines.append(f"- [{criterion.status.value}] {criterion.id}: {criterion.statement}")
    return "\n".join(lines)


def _render_checkpoint(checkpoint: TaskCheckpoint) -> str:  # noqa: C901 - one line per field
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
    # R9-163: the obstacle that stopped the last attempt. A leg resumed after a park reads
    # its predecessor's checkpoint in full and, without this, was told everything EXCEPT the
    # thing that stopped it: it re-ran the same approach into the same wall. Rendered after
    # the open questions and before the pointers, so the established findings still lead.
    if checkpoint.blocked_on:
        lines.append(f"BLOCKED ON: {checkpoint.blocked_on}")
    if checkpoint.artifact_pointers:
        lines.append("ARTIFACTS:")
        lines.extend(f"- {p.kind}: {p.ref}" for p in checkpoint.artifact_pointers)
    # Spec W1 (D-W1-16): what earlier legs already asked and already read. Rendered LAST in
    # the block so the conclusions (what is established) still lead; this is bookkeeping the
    # leg consults before it looks anything up.
    if checkpoint.queries_run:
        lines.append("QUERIES ALREADY RUN:")
        lines.extend(f"- {q}" for q in checkpoint.queries_run)
    if checkpoint.sources_seen:
        lines.append("SOURCES ALREADY SEEN:")
        lines.extend(f"- {source}" for source in checkpoint.sources_seen)
    return "\n".join(lines)


def _render_recent_legs(recent_legs: Sequence[RecentLegSummary]) -> str:
    return "\n".join(f"- {s.leg_id} ({s.outcome}): {s.summary}" for s in recent_legs)


def _render_retrieval(retrieval: Sequence[str]) -> str:
    return "\n".join(f"- {snippet}" for snippet in retrieval)


def _render_trigger(
    trigger: ScheduledFire
    | UserReply
    | UserDispatch
    | AutoRetry
    | Revived
    | EventTrigger
    | EventFire,
) -> str:
    if isinstance(trigger, ScheduledFire):
        return (
            f"TRIGGER: scheduled fire (schedule {trigger.schedule_id}) "
            f"at {trigger.fire_time.isoformat()}"
        )
    if isinstance(trigger, UserReply):
        return f"TRIGGER: the user replied: {trigger.reply}"
    if isinstance(trigger, UserDispatch):  # W1: a one-off the user asked for directly
        return f"TRIGGER: the user asked for this now, at {trigger.dispatched_at.isoformat()}"
    if isinstance(trigger, Revived):  # W1: put back after nothing was running it
        return (
            f"TRIGGER: this was put back to work ({trigger.reason}). Nothing has failed and "
            "nobody has answered you; carry on from where it stopped."
        )
    if isinstance(trigger, AutoRetry):  # W1: the system's own second attempt, said plainly
        return (
            "TRIGGER: retried automatically after a temporary failure "
            f"({trigger.cause}). Nobody has answered you; pick the work up where it broke."
        )
    if isinstance(trigger, EventFire):  # A7's on-event fire — the legible "ran because" (A7-D-6)
        return f"TRIGGER: event ({trigger.event_kind}) — {trigger.human}"
    return f"TRIGGER: event from {trigger.source}: {trigger.payload}"


def reconstruct_context(
    *,
    contract: Contract,
    trigger: ScheduledFire
    | UserReply
    | UserDispatch
    | AutoRetry
    | Revived
    | EventTrigger
    | EventFire,
    checkpoint: TaskCheckpoint | None = None,
    recent_legs: Sequence[RecentLegSummary] = (),
    retrieval: Sequence[str] = (),
) -> tuple[ReconstructionBlock, ...]:
    """Assemble a leg's context in the fixed D-A2-3 order.

    The contract always leads; the trigger always appears; the next-step (if any) is recited
    last. Optional sections (checkpoint, recent legs, retrieval) are omitted when absent —
    never reordered.

    Args:
        contract: The A4-authored anchor (re-read every leg). The METHOD block follows it
            unconditionally (D-W1-12).
        trigger: What woke this leg (the fire / reply / event).
        checkpoint: The latest checkpoint, or ``None`` on the first leg.
        recent_legs: The last-N leg summaries (already bounded by the caller).
        retrieval: Live K3/memory snippets the runtime fetched for this step.

    Returns:
        The ordered reconstruction blocks the leg renders into the loop's input.
    """
    blocks: list[ReconstructionBlock] = [
        ReconstructionBlock(stage=ReconstructionStage.CONTRACT, content=_render_contract(contract)),
        # Spec W1 (D-W1-12): how to work, every leg, right under what the work IS. Never
        # omitted: unlike the optional sections there is no "empty" case for method.
        ReconstructionBlock(stage=ReconstructionStage.METHOD, content=METHOD_BLOCK),
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
