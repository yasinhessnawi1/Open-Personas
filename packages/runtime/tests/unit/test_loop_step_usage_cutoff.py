"""Per-step usage surfacing + exhaustion cutoff through the REAL loop (Spec M3, T4a).

Drives a real :class:`AgenticLoop` (the shared ``_make_loop`` harness) and proves:

* ``on_step_usage`` fires exactly once per step, carrying the step's real token
  usage + served provider/model (what the api billing watcher prices);
* a balance-tracking watcher that flips the ``CancelToken`` on exhaustion cuts the
  run off at the NEXT step boundary — captures ≤ balance, never negative, and the
  in-flight step finishes cleanly (status CANCELLED, fewer steps than scripted).

This is the "drives the real run chain" cutoff proof (no hand-forced token flip):
the cutoff is a consequence of the per-step billing decision, through the real loop.
"""

from __future__ import annotations

import pytest
from persona_runtime.agentic.run import CancelToken, RunStatus, StepUsage
from test_loop_agentic import _make_loop, _resp  # type: ignore[import-not-found]


class _BalanceWatcher:
    """A minimal balance-tracking billing watcher (the api ``_RunBillingWatcher`` shape).

    Each step "captures" ``min(1, balance)`` (the static ``claude-sonnet-4-6`` rate
    floors to 1 credit/step); when the balance can't cover the step, it flips the
    cancel token — exactly the real watcher's exhaustion decision.
    """

    def __init__(self, *, balance: int, cancel_token: CancelToken) -> None:
        self.balance = balance
        self._cancel = cancel_token
        self.usages: list[StepUsage] = []
        self.captured_total = 0

    async def on_step_usage(self, usage: StepUsage) -> None:
        self.usages.append(usage)
        charge = 1  # the priced per-step charge for the scripted static rate
        captured = min(charge, self.balance)  # floored — never negative
        self.balance -= captured
        self.captured_total += captured
        if captured < charge or self.balance <= 0:
            self._cancel.cancel()


@pytest.mark.asyncio
async def test_on_step_usage_fires_per_step_with_real_usage() -> None:
    # Two reasoning steps then FINAL → 3 model calls → 3 usage callbacks.
    script = [_resp("thinking A"), _resp("thinking B"), _resp("[FINAL] done")]
    loop, _stores, _backend = _make_loop(script)
    token = CancelToken()
    watcher = _BalanceWatcher(balance=100, cancel_token=token)

    run = await loop.run("do it", on_step_usage=watcher.on_step_usage, cancel_token=token)

    assert run.status is RunStatus.COMPLETED
    assert len(watcher.usages) == 3  # one per step
    u = watcher.usages[0]
    assert (u.provider, u.model) == ("anthropic", "claude-sonnet-4-6")
    assert (u.prompt_tokens, u.completion_tokens) == (10, 5)  # the scripted usage
    assert u.step == 0


@pytest.mark.asyncio
async def test_run_is_cut_off_at_a_step_boundary_on_exhaustion() -> None:
    # A 6-step run, but only 3 credits of balance → the run is cut off after the
    # 3rd step (balance exhausted), BEFORE the 4th — never reaching FINAL.
    script = [_resp(f"thinking {i}") for i in range(5)] + [_resp("[FINAL] done")]
    loop, _stores, _backend = _make_loop(script, max_steps=20)
    token = CancelToken()
    watcher = _BalanceWatcher(balance=3, cancel_token=token)

    run = await loop.run("do it", on_step_usage=watcher.on_step_usage, cancel_token=token)

    assert run.status is RunStatus.CANCELLED, "an exhausted run must be cut off, not completed"
    # Exactly 3 steps ran (the balance covered 3), then the next boundary stopped it.
    assert len(run.steps) == 3
    assert len(watcher.usages) == 3
    assert watcher.captured_total == 3  # captured == balance, never more
    assert watcher.balance == 0  # floored at 0, never negative
