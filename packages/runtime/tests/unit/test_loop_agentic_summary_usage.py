"""The agentic loop's summary calls are metered like its steps (R9-299, D-M3-1).

Besides one model call per step, a run makes two other paid calls: the compaction
summary of the step history when the context nears the window (``_maybe_compact``,
the background tier), and the best-effort summary that ends a run at the step cap or
salvages a cancelled one (``_best_effort_summary``, the frontier tier). Before R9-299
neither reached ``on_step_usage``, which is the only door the api's run watcher and the
task leg's cost price through, so both were provider calls nobody was charged for.

Each test drives the REAL loop and records what ``on_step_usage`` receives. The summary
calls are served by backends with their own model names so the reported usage can be
told apart from the steps' own.
"""

# ruff: noqa: SLF001 - tests reach into the registry cache to pin the scripted backends.

from __future__ import annotations

import pytest
from _fakes import ScriptedBackend  # type: ignore[import-not-found]
from persona.backends import ChatResponse
from persona.backends.types import TokenUsage
from persona_runtime.agentic.run import CancelToken, RunStatus, StepUsage
from test_loop_agentic import _make_loop, _resp  # type: ignore[import-not-found]

pytestmark = pytest.mark.asyncio


class _Recorder:
    """Records each usage the loop reports; optionally cancels after the first."""

    def __init__(self, *, cancel_after_first: CancelToken | None = None) -> None:
        self.usages: list[StepUsage] = []
        self._cancel = cancel_after_first

    async def on_step_usage(self, usage: StepUsage) -> None:
        self.usages.append(usage)
        if self._cancel is not None:
            self._cancel.cancel()


def _summary_response(*, prompt: int, completion: int, model: str) -> ChatResponse:
    return ChatResponse(
        content="what was done and what remains",
        tool_calls=[],
        usage=TokenUsage(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        ),
        model=model,
        provider="openai",
        latency_ms=1.0,
    )


async def test_a_compaction_summary_is_reported_at_the_model_that_made_it() -> None:
    script = [_resp("thinking A"), _resp("thinking B"), _resp("thinking C"), _resp("[FINAL] ok")]
    loop, _stores, backend = _make_loop(script)
    # A tiny window, so the step history crosses the compaction threshold early.
    backend.max_tokens = 30
    # The compaction runs on the background (small) tier: its own backend, its own model.
    small = ScriptedBackend([], provider_name="openai", model_name="small-summariser")
    loop._tiers._cache["small"] = small
    recorder = _Recorder()

    run = await loop.run("do it", on_step_usage=recorder.on_step_usage)

    assert run.status is RunStatus.COMPLETED
    assert small.chat_calls >= 1, "the window never filled; this test measures nothing"
    compactions = [u for u in recorder.usages if u.call == "compaction"]
    assert len(compactions) == small.chat_calls  # every summary call reported, once
    first = compactions[0]
    assert (first.provider, first.model) == ("openai", "small-summariser")
    assert (first.prompt_tokens, first.completion_tokens) == (5, 2)  # the fake's usage
    # The steps are still reported exactly once each, as before.
    steps = [u for u in recorder.usages if u.call == "step"]
    assert len(steps) == len(run.steps)


async def test_the_step_cap_summary_is_reported() -> None:
    summary = _summary_response(prompt=700, completion=120, model="claude-sonnet-4-6")
    loop, _stores, _backend = _make_loop(
        [_resp("thinking A"), _resp("thinking B"), summary], max_steps=2
    )
    recorder = _Recorder()

    run = await loop.run("do it", on_step_usage=recorder.on_step_usage)

    assert run.status is RunStatus.MAX_STEPS_REACHED
    assert run.output == "what was done and what remains"
    assert [u.call for u in recorder.usages] == ["step", "step", "summary"]
    last = recorder.usages[-1]
    assert (last.prompt_tokens, last.completion_tokens) == (700, 120)
    assert last.step == 1  # it follows the last step that ran


async def test_the_cancellation_salvage_summary_is_reported() -> None:
    summary = _summary_response(prompt=900, completion=150, model="claude-sonnet-4-6")
    loop, _stores, _backend = _make_loop([_resp("thinking A"), summary])
    token = CancelToken()
    recorder = _Recorder(cancel_after_first=token)

    run = await loop.run("do it", on_step_usage=recorder.on_step_usage, cancel_token=token)

    assert run.status is RunStatus.CANCELLED
    assert run.output == "what was done and what remains"
    assert [u.call for u in recorder.usages] == ["step", "summary"]
    assert (recorder.usages[-1].prompt_tokens, recorder.usages[-1].completion_tokens) == (900, 150)


async def test_a_billing_failure_on_the_salvage_summary_keeps_the_salvage() -> None:
    """The salvage is the point of the cancellation path (R9-109): a meter that raises on
    it must not throw away the summary the owner has just paid a model for."""
    summary = _summary_response(prompt=900, completion=150, model="claude-sonnet-4-6")
    loop, _stores, _backend = _make_loop([_resp("thinking A"), summary])
    token = CancelToken()
    seen: list[str] = []

    async def meter(usage: StepUsage) -> None:
        seen.append(usage.call)
        token.cancel()
        if usage.call == "summary":
            raise RuntimeError("ledger unavailable")

    run = await loop.run("do it", on_step_usage=meter, cancel_token=token)

    assert seen == ["step", "summary"]
    assert run.status is RunStatus.CANCELLED
    assert run.output == "what was done and what remains"


async def test_a_run_without_a_meter_still_summarises() -> None:
    """No ``on_step_usage`` (the unit-test and unmetered shape) is unchanged."""
    summary = _summary_response(prompt=700, completion=120, model="claude-sonnet-4-6")
    loop, _stores, _backend = _make_loop([_resp("thinking A"), summary], max_steps=1)

    run = await loop.run("do it")

    assert run.status is RunStatus.MAX_STEPS_REACHED
    assert run.output == "what was done and what remains"
