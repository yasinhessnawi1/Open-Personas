"""A task leg is labelled by the least certain basis among its priced steps (D3).

A leg is several model steps, and since R9-296 each is priced at the model that served
it, so one leg can hold an OpenRouter actual and a static estimate. The leg used to take
the LAST priced step's basis, so a leg that ended on an actual was labelled an actual
although part of it was estimated. It now takes the same rule as a chat turn
(``persona_runtime.cost.least_certain_basis``): an actual only when every priced step
was one, ``unpriced`` only when no step was priced.
"""

from __future__ import annotations

import pytest
from persona_api.services.llm_usage_collector import collect_llm_usage
from persona_api.tasks.handler import _LegCost
from persona_runtime.agentic.run import StepUsage

#: In the static table (0.30 / 1.50 cents per 1k): 1000 in, 100 out = 0.45 cents.
_STATIC = ("anthropic", "claude-sonnet-4-6", None)
#: An OpenRouter actual: 0.002 USD = 0.2 cents.
_ACTUAL = ("openrouter", "z-ai/glm-4.6", 0.002)
_UNPRICED = ("vendor", "no-such-model", None)


async def _leg(*steps: tuple[str, str, float | None]) -> tuple[float, str | None]:
    with collect_llm_usage() as distillation:
        cost = _LegCost(None, distillation)
        for index, (provider, model, cost_usd) in enumerate(steps):
            await cost.on_step_usage(
                StepUsage(
                    step=index,
                    provider=provider,
                    model=model,
                    prompt_tokens=1000,
                    completion_tokens=100,
                    cost_usd=cost_usd,
                )
            )
        return cost.result()


@pytest.mark.asyncio
async def test_an_estimated_step_then_an_actual_step_is_labelled_the_estimate() -> None:
    cents, basis = await _leg(_STATIC, _ACTUAL)
    assert cents == pytest.approx(0.65)
    assert basis == "estimate_static"


@pytest.mark.asyncio
async def test_an_actual_step_then_an_estimated_step_is_labelled_the_estimate() -> None:
    assert (await _leg(_ACTUAL, _STATIC))[1] == "estimate_static"


@pytest.mark.asyncio
async def test_a_leg_of_actuals_is_labelled_an_actual() -> None:
    assert (await _leg(_ACTUAL, _ACTUAL))[1] == "actual_openrouter"


@pytest.mark.asyncio
async def test_an_unpriced_step_never_relabels_a_priced_leg() -> None:
    assert (await _leg(_STATIC, _UNPRICED))[1] == "estimate_static"
    assert (await _leg(_UNPRICED, _ACTUAL))[1] == "actual_openrouter"


@pytest.mark.asyncio
async def test_a_leg_with_no_priced_step_is_unpriced_and_one_with_no_step_has_none() -> None:
    assert (await _leg(_UNPRICED, _UNPRICED))[1] == "unpriced"
    assert await _leg() == (0.0, None)


@pytest.mark.asyncio
async def test_a_steps_failed_try_that_cost_tokens_is_in_the_legs_cost() -> None:
    """D-ORCH-11-18-s: the static step (0.45) plus its failed try at Sonnet (1000 in, 0.3)."""
    from persona.backends.types import BackendAttempt, TokenUsage

    wasted = BackendAttempt(
        index=0,
        provider="anthropic",
        model="claude-sonnet-4-6",
        error_class="EmptyCompletionError",
        usage=TokenUsage(prompt_tokens=1000, completion_tokens=0, total_tokens=1000),
    )
    with collect_llm_usage() as distillation:
        cost = _LegCost(None, distillation)
        await cost.on_step_usage(
            StepUsage(
                step=0,
                provider="anthropic",
                model="claude-sonnet-4-6",
                prompt_tokens=1000,
                completion_tokens=100,
                failed_tries=(wasted,),
            )
        )
        cents, basis = cost.result()
    assert cents == pytest.approx(0.75)
    assert basis == "estimate_static"


@pytest.mark.asyncio
async def test_the_distillers_failed_try_that_cost_tokens_is_in_the_legs_cost() -> None:
    """The distiller's own call (D-W1-44) is charged for its failed try too."""
    from persona.backends.types import BackendAttempt, TokenUsage

    wasted = BackendAttempt(
        index=0,
        provider="anthropic",
        model="claude-sonnet-4-6",
        error_class="EmptyCompletionError",
        usage=TokenUsage(prompt_tokens=1000, completion_tokens=0, total_tokens=1000),
    )
    with collect_llm_usage() as distillation:
        cost = _LegCost(None, distillation)
        distillation.record(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=1000,
            completion_tokens=100,
            cost_usd=None,
            failed_tries=(wasted,),
        )
        cents, basis = cost.result()
    assert cents == pytest.approx(0.75)  # the distillation 0.45 plus its failed try 0.3
    assert basis == "estimate_static"


def _zero_try() -> object:
    from persona.backends.types import BackendAttempt, TokenUsage

    return BackendAttempt(
        index=0,
        provider="anthropic",
        model="claude-sonnet-4-6",
        error_class="EmptyCompletionError",
        usage=TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
    )


@pytest.mark.asyncio
async def test_a_leg_with_no_step_and_no_distiller_tokens_costs_nothing() -> None:
    """The distiller guard: no tokens and no failed try that reported any is (0.0, None)."""
    with collect_llm_usage() as distillation:
        cost = _LegCost(None, distillation)
        assert cost.result() == (0.0, None)


@pytest.mark.asyncio
async def test_a_distiller_failed_try_with_no_tokens_bills_nothing() -> None:
    """A provider that sent no usage gives the try TokenUsage(0, 0, 0): it cost nothing,
    so a leg with no step and only that try is not billed (no basis, no floor)."""
    with collect_llm_usage() as distillation:
        cost = _LegCost(None, distillation)
        distillation.record(
            provider="anthropic",
            model="claude-sonnet-4-6",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=None,
            failed_tries=(_zero_try(),),  # type: ignore[arg-type]
        )
        assert cost.result() == (0.0, None)


@pytest.mark.asyncio
async def test_a_legs_steps_are_summed_and_rounded_once_before_the_ceil() -> None:
    """0.1 + 2.7 + 0.2 cents is 3.0000000000000004 in floats, which ceils to 4 credits.

    The leg total is rounded once to micro-cents, so it is 3.0 and bills 3.
    """
    from persona.billing import BillingConfig, credits_charged

    steps = [("openrouter", "z-ai/glm-4.6", usd) for usd in (0.001, 0.027, 0.002)]
    cents, basis = await _leg(*steps)
    assert cents == 3.0
    assert basis == "actual_openrouter"
    charge = credits_charged(
        provider_cents=cents, infra_flat_cents=0.0, markup=BillingConfig().credit_markup, floor=1
    )
    assert charge == 3


@pytest.mark.asyncio
async def test_a_legs_summary_calls_are_in_the_legs_cost() -> None:
    """R9-299: a leg's compaction and best-effort summary calls arrive through
    ``on_step_usage`` beside its steps, and the leg's one charge sums them all."""
    with collect_llm_usage() as distillation:
        cost = _LegCost(None, distillation)
        for call in ("step", "compaction", "summary"):
            await cost.on_step_usage(
                StepUsage(
                    step=0,
                    provider="anthropic",
                    model="claude-sonnet-4-6",
                    prompt_tokens=1000,
                    completion_tokens=100,
                    cost_usd=None,
                    call=call,  # type: ignore[arg-type]
                )
            )
        cents, basis = cost.result()
    assert cents == pytest.approx(3 * 0.45)
    assert basis == "estimate_static"
