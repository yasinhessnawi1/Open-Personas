"""The task ledger's unit: a leg's spend is money, not tokens (R9-161).

The per-task budget cap is written in currency. ``ContractBounds.total_budget_micros`` is
set from a conversational spend grant, the Tasks surface renders it as an amount of money,
and ``BudgetEnforcer.check`` compares ``task.ledger.total_micros`` straight against it. The
ledger, however, was fed by a stand-in meter that returned ``sum(step.tokens)``, and no
caller ever replaced it, so the bound a user set in money was enforced against a token
count. Billing was never wrong: it prices the same leg correctly through a path of its own,
which is exactly why nothing caught this.

What these pin:

1. the leg's priced cost and its token count are DIFFERENT numbers, and it is the priced one
   that reaches the ledger;
2. the executor has no default meter at all, so a caller cannot silently fall back to the
   wrong unit again;
3. ``micros_from_cents`` is the one conversion, it rounds up, and it never goes negative.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.tasks import (
    Contract,
    ContractBounds,
    LegBox,
    ScheduledFire,
    SpendKind,
    Task,
    TaskCheckpoint,
    micros_from_cents,
)
from persona_api.services.llm_usage_collector import collect_llm_usage
from persona_api.tasks.handler import _LegCost
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import CancelToken, Run, RunStatus, StepUsage
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import BasicCheckpointWriter, LegExecutor

_NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
_TRIGGER = ScheduledFire(schedule_id="sched-1", fire_time=_NOW)

#: The leg this file drives: an OpenRouter response-side actual of 0.04 USD across a run of
#: 9 000 tokens. Four cents is 400 ledger micros; the token count is 9 000. Two numbers far
#: enough apart that no rounding rule can confuse them, and in the direction that matters:
#: under the old meter the ledger read 22x what the leg really cost.
_COST_USD = 0.04
_COST_MICROS = 400
_RUN_TOKENS = 9_000


class _PricedRunner:
    """A finished leg that reports one real, priceable model call."""

    async def run(
        self,
        task: str,
        *,
        on_event,  # noqa: ANN001 (part of the runner contract)
        cancel_token: CancelToken,  # noqa: ARG002 (part of the runner contract)
        on_step_usage=None,  # noqa: ANN001 (part of the runner contract)
    ) -> Run:
        await on_event(RunEvent.thinking(0))
        if on_step_usage is not None:
            await on_step_usage(
                StepUsage(
                    step=0,
                    provider="openrouter",
                    model="z-ai/glm-4.6",
                    prompt_tokens=8_000,
                    completion_tokens=1_000,
                    cost_usd=_COST_USD,
                )
            )
        return Run(
            persona_id="persona_a",
            task=task,
            status=RunStatus.COMPLETED,
            steps=[Step(type=StepType.FINAL, content="done", tokens=_RUN_TOKENS)],
            output="done",
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


def _task(cap_micros: int | None = None) -> Task:
    bounds = ContractBounds(total_budget_micros=cap_micros)
    return Task(
        id="t1",
        owner_id="user_a",
        persona_id="persona_a",
        contract=Contract(goal="book the cheapest fare", bounds=bounds),
        state="active",
        created_at=_NOW,
        updated_at=_NOW,
    )


async def _run_one_leg(task: Task) -> tuple[Task, dict[SpendKind, int]]:
    """Drive a real leg through the real meter the api handler injects."""
    sink = _RecordingSink()
    with collect_llm_usage() as distillation:
        cost = _LegCost(None, distillation)
        executor = LegExecutor(
            runner=_PricedRunner(),
            writer=BasicCheckpointWriter(),
            sink=sink,
            meter=cost.ledger_spend,
        )
        outcome = await executor.run_leg(
            task=task,
            trigger=_TRIGGER,
            box=LegBox(),
            now=_NOW,
            on_step_usage=cost.on_step_usage,
        )
    return outcome.task, sink.spend


@pytest.mark.asyncio
async def test_the_ledger_accrues_the_legs_money_not_its_tokens() -> None:
    """The load-bearing one. The leg cost 4¢ and burned 9 000 tokens; the ledger takes 400
    micros. Under the stand-in meter it took 9 000, which is the same field, the same
    comparison, and a completely different promise to the user."""
    task, spend = await _run_one_leg(_task())

    assert spend == {SpendKind.MODEL: _COST_MICROS}
    assert task.ledger.model_micros == _COST_MICROS
    assert _COST_MICROS != _RUN_TOKENS, "the fixture must keep money and tokens apart"


@pytest.mark.asyncio
async def test_a_cap_set_to_the_legs_real_cost_is_reached_and_a_token_sized_one_is_not() -> None:
    """The cap's unit, end to end: contract bound → ledger → the enforcer's comparison.

    ``BudgetEnforcer.check`` is one line over these two numbers (``spent >= cap``), and the
    integration test drives the real enforcer against real Postgres. What is pinned here is
    the pair that decides whether the bound means anything: a cap set to what the leg really
    costs is reached, and a cap set to the leg's TOKEN count, the number the ledger used to
    carry and roughly 22x the real cost here, is nowhere near it.
    """
    spent_against_money_cap = (await _run_one_leg(_task(cap_micros=_COST_MICROS)))[0]
    spent_against_token_cap = (await _run_one_leg(_task(cap_micros=_RUN_TOKENS)))[0]

    money_cap = spent_against_money_cap.contract.bounds.total_budget_micros
    token_cap = spent_against_token_cap.contract.bounds.total_budget_micros
    assert money_cap is not None
    assert token_cap is not None
    assert spent_against_money_cap.ledger.total_micros >= money_cap  # REACHED
    assert spent_against_token_cap.ledger.total_micros < token_cap  # OK, nowhere near


@pytest.mark.asyncio
async def test_the_unrecorded_spend_kinds_stay_honestly_zero() -> None:
    """``SANDBOX`` and ``EXTERNAL`` are zero because nothing reports a per-leg figure for
    them, not because a leg is free of them: a sandbox execution bills the owner from inside
    the tool and tells the enclosing leg nothing, and connector / MCP infra is subsumed by
    the leg's own credit floor rather than charged per call. A fabricated number in either
    column would read as a measurement."""
    task, _ = await _run_one_leg(_task())

    assert task.ledger.sandbox_micros == 0
    assert task.ledger.external_micros == 0


def test_the_executor_refuses_to_run_without_a_meter() -> None:
    """The shape that keeps this fixed. The bug existed because a wrong default was
    silently acceptable for the whole life of the feature, so there is no default any
    more: a caller that does not say what unit it meters in does not get an executor."""
    with pytest.raises(TypeError, match="meter"):
        LegExecutor(  # type: ignore[call-arg]
            runner=_PricedRunner(), writer=BasicCheckpointWriter(), sink=_RecordingSink()
        )


@pytest.mark.parametrize(
    ("cents", "micros"),
    [
        (0.0, 0),
        (1.0, 100),
        (0.04 * 100, 400),  # the leg above: 0.04 USD → 4¢ → 400 micros
        (0.000_1, 1),  # a hundredth of a cent still costs a micro (rounded up, never lost)
        (-5.0, 0),  # defensive: a bad upstream value never credits back against the cap
    ],
)
def test_cents_convert_to_micros_once_and_round_up(cents: float, micros: int) -> None:
    assert micros_from_cents(cents) == micros
