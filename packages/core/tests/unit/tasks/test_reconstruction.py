"""Unit tests for context reconstruction ordering (Spec A2, T3; D-A2-3).

The fixed per-leg bootstrap: **contract → checkpoint → last-N leg summaries → live
retrieval → the trigger → re-plan + recite next-step last**. The contract leads (the
anti-drift re-anchor at the high-attention head); the next-step is recited last (the
high-attention tail, the anti-ossification recitation). This is the ordering LOCK — the
tests pin the order exactly, and that optional sections are omitted, not reordered.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.tasks import (
    METHOD_BLOCK,
    Contract,
    Decision,
    Deliverable,
    DeliverableFormat,
    RecentLegSummary,
    ReconstructionBlock,
    ReconstructionStage,
    ScheduledFire,
    TaskCheckpoint,
    UserReply,
    reconstruct_context,
)
from pydantic import ValidationError

_NOW = datetime(2026, 6, 25, 7, 0, tzinfo=UTC)
_CONTRACT = Contract(goal="find the cheapest Oslo→Bergen fare this week")
_TRIGGER = UserReply(reply="yes, Tuesday works")


def _checkpoint(**overrides: object) -> TaskCheckpoint:
    base: dict[str, object] = {
        "task_id": "task-1",
        "leg_id": "leg-2",
        "checkpoint_seq": 1,
        "progress_conclusions": ("best fare so far 1620kr (SAS, Tue)",),
        "next_step": "re-check Wed 07:00",
        "updated_at": _NOW,
    }
    base.update(overrides)
    return TaskCheckpoint(**base)  # type: ignore[arg-type]


def _stages(blocks: tuple[ReconstructionBlock, ...]) -> list[ReconstructionStage]:
    return [b.stage for b in blocks]


def test_first_leg_is_contract_then_method_then_trigger() -> None:
    # No checkpoint yet (first leg), no recent summaries, no retrieval. METHOD is there even
    # on the very first leg: how to work is not something a run earns by its second attempt.
    blocks = reconstruct_context(contract=_CONTRACT, trigger=_TRIGGER)
    assert _stages(blocks) == [
        ReconstructionStage.CONTRACT,
        ReconstructionStage.METHOD,
        ReconstructionStage.TRIGGER,
    ]


def test_full_reconstruction_order_is_exact() -> None:
    blocks = reconstruct_context(
        contract=_CONTRACT,
        trigger=_TRIGGER,
        checkpoint=_checkpoint(),
        recent_legs=(
            RecentLegSummary(leg_id="leg-1", summary="surveyed airlines", outcome="done"),
        ),
        retrieval=("graph: user dislikes redeye flights",),
    )
    assert _stages(blocks) == [
        ReconstructionStage.CONTRACT,
        ReconstructionStage.METHOD,
        ReconstructionStage.CHECKPOINT,
        ReconstructionStage.RECENT_LEGS,
        ReconstructionStage.RETRIEVAL,
        ReconstructionStage.TRIGGER,
        ReconstructionStage.RECITE_NEXT_STEP,
    ]


def test_contract_is_always_first() -> None:
    blocks = reconstruct_context(contract=_CONTRACT, trigger=_TRIGGER, checkpoint=_checkpoint())
    assert blocks[0].stage == ReconstructionStage.CONTRACT


def test_next_step_is_recited_last() -> None:
    blocks = reconstruct_context(contract=_CONTRACT, trigger=_TRIGGER, checkpoint=_checkpoint())
    assert blocks[-1].stage == ReconstructionStage.RECITE_NEXT_STEP
    assert "re-check Wed 07:00" in blocks[-1].content


def test_no_recite_block_without_a_next_step() -> None:
    blocks = reconstruct_context(
        contract=_CONTRACT, trigger=_TRIGGER, checkpoint=_checkpoint(next_step="")
    )
    assert ReconstructionStage.RECITE_NEXT_STEP not in _stages(blocks)
    # the trigger is then the tail.
    assert blocks[-1].stage == ReconstructionStage.TRIGGER


def test_optional_sections_are_omitted_not_reordered() -> None:
    # checkpoint + retrieval, but NO recent_legs → retrieval still sits after where recent
    # would be and before the trigger; nothing is reordered.
    blocks = reconstruct_context(
        contract=_CONTRACT,
        trigger=_TRIGGER,
        checkpoint=_checkpoint(),
        retrieval=("a retrieved fact",),
    )
    assert _stages(blocks) == [
        ReconstructionStage.CONTRACT,
        ReconstructionStage.METHOD,
        ReconstructionStage.CHECKPOINT,
        ReconstructionStage.RETRIEVAL,
        ReconstructionStage.TRIGGER,
        ReconstructionStage.RECITE_NEXT_STEP,
    ]


def test_empty_retrieval_and_recent_are_omitted() -> None:
    blocks = reconstruct_context(
        contract=_CONTRACT, trigger=_TRIGGER, checkpoint=_checkpoint(), recent_legs=(), retrieval=()
    )
    assert ReconstructionStage.RETRIEVAL not in _stages(blocks)
    assert ReconstructionStage.RECENT_LEGS not in _stages(blocks)


def test_trigger_is_always_present() -> None:
    blocks = reconstruct_context(
        contract=_CONTRACT,
        trigger=ScheduledFire(schedule_id="sched-1", fire_time=_NOW),
        checkpoint=_checkpoint(),
    )
    trigger_blocks = [b for b in blocks if b.stage == ReconstructionStage.TRIGGER]
    assert len(trigger_blocks) == 1


# --- content rendering (faithful, deterministic) -----------------------------


def test_contract_block_renders_the_goal() -> None:
    blocks = reconstruct_context(contract=_CONTRACT, trigger=_TRIGGER)
    contract_block = next(b for b in blocks if b.stage == ReconstructionStage.CONTRACT)
    assert "find the cheapest Oslo→Bergen fare" in contract_block.content


def test_checkpoint_block_renders_conclusions_and_plan() -> None:
    cp = _checkpoint(
        decisions=(
            Decision(decision="exclude redeye", rationale="user sleeps poorly", leg_id="leg-1"),
        ),
    )
    blocks = reconstruct_context(contract=_CONTRACT, trigger=_TRIGGER, checkpoint=cp)
    cp_block = next(b for b in blocks if b.stage == ReconstructionStage.CHECKPOINT)
    assert "best fare so far 1620kr" in cp_block.content
    assert "exclude redeye" in cp_block.content


def test_trigger_block_renders_user_reply() -> None:
    blocks = reconstruct_context(contract=_CONTRACT, trigger=UserReply(reply="Tuesday is fine"))
    trigger_block = next(b for b in blocks if b.stage == ReconstructionStage.TRIGGER)
    assert "Tuesday is fine" in trigger_block.content


def test_trigger_block_renders_scheduled_fire() -> None:
    blocks = reconstruct_context(
        contract=_CONTRACT, trigger=ScheduledFire(schedule_id="sched-9", fire_time=_NOW)
    )
    trigger_block = next(b for b in blocks if b.stage == ReconstructionStage.TRIGGER)
    assert "sched-9" in trigger_block.content


def test_recent_legs_block_renders_summaries() -> None:
    blocks = reconstruct_context(
        contract=_CONTRACT,
        trigger=_TRIGGER,
        checkpoint=_checkpoint(),
        recent_legs=(
            RecentLegSummary(leg_id="leg-1", summary="surveyed 3 airlines", outcome="done"),
        ),
    )
    recent_block = next(b for b in blocks if b.stage == ReconstructionStage.RECENT_LEGS)
    assert "surveyed 3 airlines" in recent_block.content


# --- value-type shapes --------------------------------------------------------


def test_recent_leg_summary_is_frozen_and_forbids_extra() -> None:
    s = RecentLegSummary(leg_id="l", summary="x", outcome="done")
    with pytest.raises(ValidationError):
        s.summary = "y"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        RecentLegSummary(leg_id="l", summary="x", outcome="done", extra="no")  # type: ignore[call-arg]


def test_reconstruction_block_is_frozen() -> None:
    block = ReconstructionBlock(stage=ReconstructionStage.CONTRACT, content="x")
    with pytest.raises(ValidationError):
        block.content = "y"  # type: ignore[misc]


def test_trigger_block_renders_user_dispatch() -> None:
    """Spec W1: a one-off's first leg names when the user asked, and nothing else.

    The brief is already the contract goal at the head, so the trigger must not repeat it.
    """
    from datetime import UTC, datetime

    from persona.tasks import Contract, UserDispatch, reconstruct_context

    at = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    blocks = reconstruct_context(
        contract=Contract(goal="list the three newest AI persona repos"),
        trigger=UserDispatch(dispatched_at=at),
    )
    trigger_block = [b for b in blocks if b.stage.value == "trigger"][0]
    assert trigger_block.content == f"TRIGGER: the user asked for this now, at {at.isoformat()}"
    assert "newest AI persona repos" not in trigger_block.content


# --- METHOD (Spec W1, D-W1-12) ----------------------------------------------


def test_method_sits_between_the_contract_and_everything_else() -> None:
    """D-W1-12's ordering: what the work IS, then how to work, then the work's state. The
    head of the context is where a model attends, and method is useless after the fact."""
    blocks = reconstruct_context(contract=_CONTRACT, trigger=_TRIGGER, checkpoint=_checkpoint())

    assert blocks[0].stage is ReconstructionStage.CONTRACT
    assert blocks[1].stage is ReconstructionStage.METHOD


def test_the_method_block_carries_the_rules_a_leg_keeps_breaking() -> None:
    """Each line is a failure that was actually observed: one lookup per step, the same
    failing call retried, a query already run three legs ago, and a leg that hit its
    wall-clock with everything it had learned still in its head."""
    blocks = reconstruct_context(contract=_CONTRACT, trigger=_TRIGGER)
    method = next(b for b in blocks if b.stage is ReconstructionStage.METHOD).content

    assert "ONE step" in method  # batch independent lookups
    assert "Never repeat a call that failed" in method
    assert "Do not run them again" in method  # the ledger below is not decoration
    assert "before your time runs out" in method  # partial findings beat none


def test_the_method_block_is_the_same_every_leg() -> None:
    """Universal, never per-task: the contract says what, this says how. A per-task method
    would be a second place for the goal to drift."""
    first = reconstruct_context(contract=_CONTRACT, trigger=_TRIGGER)
    later = reconstruct_context(
        contract=_CONTRACT, trigger=_TRIGGER, checkpoint=_checkpoint(), retrieval=("a fact",)
    )

    assert first[1].content == later[1].content == METHOD_BLOCK


def test_the_contract_block_names_the_agreed_shape() -> None:
    """Spec W1 (T11): "done" has to mean the same thing on leg 6 as it did on leg 1."""
    blocks = reconstruct_context(
        contract=Contract(
            goal="compare the plans",
            deliverable=Deliverable(format=DeliverableFormat.TABLE),
        ),
        trigger=_TRIGGER,
    )

    assert "DELIVERABLE: table" in blocks[0].content


def test_the_ledgers_are_rendered_under_the_conclusions() -> None:
    """Spec W1 (D-W1-16): the conclusions lead (what is established); the bookkeeping the
    leg consults before it looks anything up follows."""
    blocks = reconstruct_context(
        contract=_CONTRACT,
        trigger=_TRIGGER,
        checkpoint=_checkpoint(
            queries_run=("husleieloven deposit interest",),
            sources_seen=("https://lovdata.no/a",),
        ),
    )
    checkpoint_block = next(b for b in blocks if b.stage is ReconstructionStage.CHECKPOINT).content

    assert "CONCLUSIONS:" in checkpoint_block
    assert checkpoint_block.index("CONCLUSIONS:") < checkpoint_block.index("QUERIES ALREADY RUN:")
    assert "husleieloven deposit interest" in checkpoint_block
    assert "https://lovdata.no/a" in checkpoint_block


def test_a_checkpoint_with_no_ledgers_renders_no_ledger_headings() -> None:
    blocks = reconstruct_context(contract=_CONTRACT, trigger=_TRIGGER, checkpoint=_checkpoint())
    checkpoint_block = next(b for b in blocks if b.stage is ReconstructionStage.CHECKPOINT).content

    assert "QUERIES ALREADY RUN:" not in checkpoint_block
    assert "SOURCES ALREADY SEEN:" not in checkpoint_block
