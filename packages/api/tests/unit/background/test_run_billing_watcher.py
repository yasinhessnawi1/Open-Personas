"""The agentic-run billing watcher (Spec M3, T4a).

``_RunBillingWatcher.on_step_usage`` prices a step's usage like a chat turn,
captures it incrementally (floored, caller-paid), records ``cost_cents``/
``cost_basis`` with a basis-carrying reason, and flips the run's ``CancelToken``
when the balance can't cover the step (partial capture / balance 0 / day-cap) —
the exhaustion decision the real loop's cutoff rides on.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.billing import BillingConfig
from persona.errors import DailySpendCapExceededError
from persona_api.background.run_worker import _RunBillingWatcher
from persona_runtime.agentic.run import CancelToken, StepUsage

_ENGINE: Any = object()


class _FakePolicy:
    """Records capture calls; returns a scripted (captured, new_balance)."""

    def __init__(self, *, captured: int, new_balance: int, day_cap: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._captured = captured
        self._new_balance = new_balance
        self._day_cap = day_cap

    def capture_up_to(self, **kw: object) -> tuple[int, int]:
        self.calls.append(kw)
        if self._day_cap:
            raise DailySpendCapExceededError("capped", context={"cap": "5"})
        return self._captured, self._new_balance


def _watcher(policy: _FakePolicy, token: CancelToken, *, floor: int = 1) -> _RunBillingWatcher:
    return _RunBillingWatcher(
        policy=policy,  # type: ignore[arg-type]
        rls_engine=_ENGINE,
        owner_id="u1",
        cancel_token=token,
        cost_source=None,
        billing_config=BillingConfig(),
        floor=floor,
    )


def _usage(
    *, provider: str = "openrouter", model: str = "x", cost_usd: float | None = 0.03
) -> StepUsage:
    return StepUsage(
        step=0,
        provider=provider,
        model=model,
        prompt_tokens=100,
        completion_tokens=1000,
        cost_usd=cost_usd,
    )


@pytest.mark.asyncio
async def test_normal_step_captures_the_real_cost_no_cutoff() -> None:
    policy = _FakePolicy(captured=3, new_balance=97)  # 3¢ actual → 3 credits, fully covered
    token = CancelToken()
    await _watcher(policy, token).on_step_usage(_usage())
    kw = policy.calls[-1]
    assert kw["amount"] == 3  # ceil(3.0¢)
    assert kw["reason"] == "agentic_run:actual_openrouter"
    assert kw["cost_cents"] == pytest.approx(3.0)
    assert kw["cost_basis"] == "actual_openrouter"
    assert not token.is_cancelled  # balance covered the step → run continues


@pytest.mark.asyncio
async def test_partial_capture_cuts_off() -> None:
    # The balance only covered 1 of the 3 credits → exhausted → cut off.
    policy = _FakePolicy(captured=1, new_balance=0)
    token = CancelToken()
    await _watcher(policy, token).on_step_usage(_usage())
    assert token.is_cancelled


@pytest.mark.asyncio
async def test_zero_balance_after_capture_cuts_off() -> None:
    policy = _FakePolicy(captured=3, new_balance=0)  # fully captured but now empty
    token = CancelToken()
    await _watcher(policy, token).on_step_usage(_usage())
    assert token.is_cancelled  # nothing left for the next step → stop at the boundary


@pytest.mark.asyncio
async def test_day_cap_refusal_cuts_off() -> None:
    policy = _FakePolicy(captured=0, new_balance=0, day_cap=True)
    token = CancelToken()
    await _watcher(policy, token).on_step_usage(_usage())
    assert token.is_cancelled


@pytest.mark.asyncio
async def test_unpriced_step_falls_to_the_floor() -> None:
    # An off-table model + no usage.cost → unpriced → the floor (1 credit) is charged.
    policy = _FakePolicy(captured=1, new_balance=50)
    token = CancelToken()
    await _watcher(policy, token).on_step_usage(
        _usage(provider="fake", model="fake-1", cost_usd=None)
    )
    kw = policy.calls[-1]
    assert kw["amount"] == 1  # the floor
    assert kw["cost_basis"] == "unpriced"
    assert not token.is_cancelled


@pytest.mark.asyncio
async def test_a_steps_failed_try_that_cost_tokens_is_in_the_charge() -> None:
    """D-ORCH-11-18-s: 3.0 cents for the answer plus 0.4 cents its failed try burned."""
    from persona.backends.types import BackendAttempt, TokenUsage

    wasted = BackendAttempt(
        index=0,
        provider="openrouter",
        model="primary",
        error_class="EmptyCompletionError",
        usage=TokenUsage(prompt_tokens=500, completion_tokens=0, total_tokens=500, cost_usd=0.004),
    )
    policy = _FakePolicy(captured=4, new_balance=96)
    usage = _usage().model_copy(update={"failed_tries": (wasted,)})
    await _watcher(policy, CancelToken()).on_step_usage(usage)
    kw = policy.calls[-1]
    assert kw["cost_cents"] == pytest.approx(3.4)
    assert kw["amount"] == 4  # one ceil over the step, failed try included
