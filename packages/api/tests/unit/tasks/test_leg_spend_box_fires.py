"""The per-LEG spend cap actually stops a leg now (R9-176).

The bound existed, was unit-tested, and had never once fired in production, for two
independent reasons that each would have been enough on their own:

1. ``LegBox.budget_micros`` defaults to ``None`` and BOTH production construction sites
   built ``LegBox()`` with no arguments, so the branch at ``boxing.py:106`` was unreachable;
2. the only code that evaluated it passed a hardcoded ``spent_micros=0``, so even a box
   carrying a budget could not have tripped.

Nothing went red, because nothing had ever been able to go red. This is the "gate without a
knocker" shape in the billing safety path, and it is the reason the ENGINEERING_STANDARDS
rule about dark code asks what makes a thing reachable rather than whether it is tested.

What these pin:

1. a leg whose real, metered spend crosses the box stops, at a step boundary, saying BUDGET;
2. the bound and the ledger read the same accumulator, so what stopped the leg and what is
   recorded against it cannot disagree;
3. the handler derives the box budget from the task's REMAINING budget, which is the wiring
   that was missing (a test of the mechanism alone would have passed all along).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.tasks import (
    Contract,
    ContractBounds,
    LegBox,
    LegBoxLimit,
    ScheduledFire,
    SpendKind,
    Task,
    TaskCheckpoint,
)
from persona_api.services.llm_usage_collector import collect_llm_usage
from persona_api.tasks.handler import _LegCost
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import CancelToken, Run, RunStatus, StepUsage
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import BasicCheckpointWriter, LegExecutor

_NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
_TRIGGER = ScheduledFire(schedule_id="sched-1", fire_time=_NOW)

#: Each step of the fixture leg costs 2 US cents, which is 200 ledger micros.
_STEP_COST_USD = 0.02
_STEP_MICROS = 200


class _PricedSteppingRunner:
    """A leg that takes several priced steps, reporting each one as it goes.

    The usage is reported BEFORE the next ``thinking`` event, which is what a real run does
    and what makes the mid-leg probe meaningful: by the time the box is consulted for step
    N, the cost of steps up to N-1 is already in the meter.
    """

    def __init__(self, steps: int) -> None:
        self._steps = steps
        self.steps_run = 0

    async def run(
        self,
        task: str,
        *,
        on_event,  # noqa: ANN001 (part of the runner contract)
        cancel_token: CancelToken,
        on_step_usage=None,  # noqa: ANN001 (part of the runner contract)
    ) -> Run:
        for index in range(self._steps):
            if cancel_token.is_cancelled:
                break
            await on_event(RunEvent.thinking(index))
            self.steps_run += 1
            if on_step_usage is not None:
                await on_step_usage(
                    StepUsage(
                        step=index,
                        provider="openrouter",
                        model="z-ai/glm-4.6",
                        prompt_tokens=4_000,
                        completion_tokens=500,
                        cost_usd=_STEP_COST_USD,
                    )
                )
        return Run(
            persona_id="persona_a",
            task=task,
            status=RunStatus.CANCELLED if cancel_token.is_cancelled else RunStatus.COMPLETED,
            steps=[
                Step(type=StepType.REASONING, content=f"step {i}", tokens=100)
                for i in range(self.steps_run)
            ],
            output="",
            started_at=_NOW,
            finished_at=_NOW,
        )


class _RecordingSink:
    """Accrues the spend into the task exactly as ``CheckpointStore.append`` does."""

    def __init__(self) -> None:
        self.spend: dict[SpendKind, int] = {}

    def append(self, task: Task, checkpoint: TaskCheckpoint, *, spend, now: datetime) -> Task:  # noqa: ANN001
        self.spend = dict(spend)
        advanced = task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)
        for kind, micros in spend.items():
            advanced = advanced.record_spend(kind, micros, now=now)
        return advanced


def _task(cap_micros: int | None = None, already_spent: int = 0) -> Task:
    task = Task(
        id="t1",
        owner_id="user_a",
        persona_id="persona_a",
        contract=Contract(
            goal="book the cheapest fare", bounds=ContractBounds(total_budget_micros=cap_micros)
        ),
        state="active",
        created_at=_NOW,
        updated_at=_NOW,
    )
    if already_spent:
        task = task.record_spend(SpendKind.MODEL, already_spent, now=_NOW)
    return task


async def _run_leg(*, budget_micros: int | None, steps: int = 6):  # noqa: ANN202
    """Drive a real leg through the real meter, with the real mid-leg probe."""
    sink = _RecordingSink()
    runner = _PricedSteppingRunner(steps)
    with collect_llm_usage() as distillation:
        cost = _LegCost(None, distillation)
        executor = LegExecutor(
            runner=runner,
            writer=BasicCheckpointWriter(),
            sink=sink,
            meter=cost.ledger_spend,
        )
        outcome = await executor.run_leg(
            task=_task(),
            trigger=_TRIGGER,
            box=LegBox(max_steps=50, wall_clock_seconds=3_600.0, budget_micros=budget_micros),
            now=_NOW,
            on_step_usage=cost.on_step_usage,
            spent_micros=cost.spent_micros,
        )
    return outcome, runner, sink


@pytest.mark.asyncio
async def test_a_leg_that_runs_past_its_budget_is_stopped_and_says_so() -> None:
    """The load-bearing one: real metered spend, real probe, the leg actually stops.

    The box allows 500 micros and each step costs 200. After two steps the meter reads 400,
    which is inside; after three it reads 600, which is not. So the leg stops there instead
    of running its full six steps, and the outcome names BUDGET rather than dying silently.
    """
    outcome, runner, sink = await _run_leg(budget_micros=500, steps=6)

    assert outcome.box_limit == LegBoxLimit.BUDGET
    assert runner.steps_run < 6, "the leg must be cut short, not run to completion"
    assert sink.spend, "a stopped leg still records what it spent"


@pytest.mark.asyncio
async def test_the_bound_and_the_ledger_read_the_same_number() -> None:
    """What stopped the leg and what is billed against it cannot drift apart.

    They go through one accumulator and one conversion. This is the property that makes the
    bound trustworthy: a cap enforced against a figure the ledger disagrees with is exactly
    the R9-161 defect wearing different clothes.
    """
    outcome, runner, sink = await _run_leg(budget_micros=500, steps=6)

    assert sink.spend[SpendKind.MODEL] == runner.steps_run * _STEP_MICROS
    assert outcome.task.ledger.total_micros == sink.spend[SpendKind.MODEL]


@pytest.mark.asyncio
async def test_a_leg_within_its_budget_runs_to_the_end() -> None:
    """The bound must not fire early: a generous cap changes nothing about the leg."""
    outcome, runner, _ = await _run_leg(budget_micros=1_000_000, steps=4)

    assert outcome.box_limit is None
    assert runner.steps_run == 4


@pytest.mark.asyncio
async def test_no_budget_on_the_box_leaves_spend_unbounded() -> None:
    """``None`` is still "no spend cap", so the pre-R9-176 shape is unchanged."""
    outcome, runner, _ = await _run_leg(budget_micros=None, steps=4)

    assert outcome.box_limit is None
    assert runner.steps_run == 4


# --- the wiring, which is the half that was actually dark ---------------------


def _handler_with_remaining(remaining: int | None):  # noqa: ANN202
    """A leg handler carrying only what ``_leg_box`` reads (the rest is not exercised)."""
    from persona_api.tasks.handler import TaskLegHandler

    handler = TaskLegHandler.__new__(TaskLegHandler)
    handler._box = LegBox(max_steps=7, wall_clock_seconds=42.0)  # noqa: SLF001
    handler._leg_budget_micros = None if remaining is None else lambda _o, _t: remaining  # noqa: SLF001
    return handler


def test_the_handler_gives_the_box_the_tasks_remaining_budget() -> None:
    """The defect itself: the box reached the executor with no budget, every single time.

    A test of ``LegBox.exhausted_by`` passed throughout, because the mechanism was correct.
    Only the wiring was missing, so only a test of the wiring could have caught it.
    """
    box = _handler_with_remaining(12_345)._leg_box("user_a", _task())  # noqa: SLF001

    assert box.budget_micros == 12_345
    assert box.max_steps == 7  # the configured box is preserved, not replaced
    assert box.wall_clock_seconds == 42.0


def test_a_task_over_its_cap_yields_a_zero_budget_rather_than_a_negative_one() -> None:
    """Over-cap means nothing left to spend, which the box must read as zero.

    A negative budget would compare as "never reached" and quietly restore the old
    unbounded behaviour in precisely the case that most needs the bound.
    """
    box = _handler_with_remaining(0)._leg_box("user_a", _task())  # noqa: SLF001

    assert box.budget_micros == 0


def test_without_a_budget_reader_the_handler_passes_its_configured_box_untouched() -> None:
    """A plain A2 worker wires no cap, and behaves exactly as it did before R9-176."""
    handler = _handler_with_remaining(None)

    assert handler._leg_box("user_a", _task()) is handler._box  # noqa: SLF001
