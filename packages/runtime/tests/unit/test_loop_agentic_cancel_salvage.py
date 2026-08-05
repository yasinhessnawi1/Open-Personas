"""A cancelled agentic run must carry its work forward (R9-109).

The production defect: a task leg is stopped by its wall-clock box, which trips the
loop's ``CancelToken`` at a step boundary. Every such leg returned ``output=None``,
so ~200s and 9 to 19 tool calls of real research evaporated. That is not merely lost
detail: BOTH checkpoint writers accumulate progress from ``run.output`` and from
nothing else, so leg N+1 inherited an empty checkpoint, replanned from the bare
contract, and reran the same searches. Observed across every long task: identical
queries every leg, no forward motion, full price each time.

Max-steps already summarised on its way out. Cancellation is the same situation and
did not. These tests pin the symmetry, its two guards (nothing to salvage before the
first step; a failed salvage must not become an error), and the continuity payoff at
the checkpoint, which is the part production actually depends on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import pytest
from persona.backends import ChatResponse  # noqa: TC002 - a return annotation on a helper
from persona.schema.tools import ToolCall
from persona.tasks import Contract, Task
from persona_runtime.agentic.events import RunEvent  # noqa: TC002 - used in a callback signature
from persona_runtime.agentic.run import CancelToken, RunStatus
from persona_runtime.legs.distiller import CompactingCheckpointWriter
from test_loop_agentic import _make_loop, _resp  # type: ignore[import-not-found]

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from persona.schema.conversation import ConversationMessage

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_SALVAGE = "Checked three sources; the 2026 rate is 4.5% and the appeal window is 30 days."


class _Watcher(Protocol):
    """A run-event callback that also reports how many steps it has seen."""

    seen: int

    def __call__(self, event: RunEvent) -> Awaitable[None]: ...


def _tool_step(call_id: str) -> ChatResponse:
    """One scripted round that calls the echo tool, i.e. one step of real work."""
    return _resp(tool_calls=[ToolCall(name="echo", args={"message": call_id}, call_id=call_id)])


def _box_watcher(token: CancelToken, *, trip_after_steps: int) -> _Watcher:
    """The production trip condition, reproduced.

    ``_BoxWatcher`` in ``legs/executor.py`` counts ``thinking`` events and cancels when
    the box is spent. Driving the REAL trigger matters here: a test that pre-cancels
    the token instead would exercise the zero-step path and never see the salvage.
    """

    async def on_event(event: RunEvent) -> None:
        if event.type != "thinking":
            return
        on_event.seen += 1  # type: ignore[attr-defined]
        if on_event.seen >= trip_after_steps:  # type: ignore[attr-defined]
            token.cancel()

    on_event.seen = 0  # type: ignore[attr-defined]
    return on_event  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_a_cancelled_run_keeps_the_work_it_did() -> None:
    """THE regression: the box trips mid-task and the findings survive."""
    # The box trips on the 2nd ``thinking`` event, so step 2 finishes and the loop
    # stops at the top of step 3: two steps of work, then the salvage call.
    script = [_tool_step("c1"), _tool_step("c2"), _resp(_SALVAGE)]
    loop, _, backend = _make_loop(script)
    token = CancelToken()
    watcher = _box_watcher(token, trip_after_steps=2)

    run = await loop.run("research the rate", cancel_token=token, on_event=watcher)

    assert run.status is RunStatus.CANCELLED
    assert run.steps, "the box must trip AFTER real work, or this asserts nothing"
    assert run.output == _SALVAGE, (
        "a cancelled leg returned no output in production, so the checkpoint carried "
        "no progress and the next leg restarted the same research from zero"
    )
    # The salvage is one extra model call beyond the steps that ran.
    assert backend.chat_calls == len(run.steps) + 1


@pytest.mark.asyncio
async def test_cancel_before_the_first_step_spends_nothing() -> None:
    """Guard one: no work done means nothing to salvage, so no model call."""
    loop, _, backend = _make_loop([_tool_step("c1")])
    token = CancelToken()
    token.cancel()

    run = await loop.run("t", cancel_token=token)

    assert run.status is RunStatus.CANCELLED
    assert run.steps == []
    assert run.output is None
    assert backend.chat_calls == 0, "summarising an empty run would bill for nothing"


@pytest.mark.asyncio
async def test_a_failed_salvage_does_not_become_an_error() -> None:
    """Guard two: cancellation is when the environment is least healthy.

    The salvage is upside; if it fails, the run must terminate exactly as it did
    before this change rather than turning a continuable leg into a FAILED one.
    """
    script = [_tool_step("c1"), _tool_step("c2")]
    loop, _, backend = _make_loop(script)
    token = CancelToken()

    real_chat = backend.chat

    async def exploding_chat(messages: list[ConversationMessage], **kw: object) -> ChatResponse:
        # The salvage is the only chat call the loop makes without tools, so this
        # breaks exactly it and leaves every step running normally.
        if kw.get("tools") is None:
            msg = "provider 503"
            raise RuntimeError(msg)
        return await real_chat(messages, **kw)  # type: ignore[arg-type]

    backend.chat = exploding_chat  # type: ignore[method-assign]
    watcher = _box_watcher(token, trip_after_steps=2)

    run = await loop.run("t", cancel_token=token, on_event=watcher)

    assert run.status is RunStatus.CANCELLED, "a failed salvage must not change the status"
    assert run.output is None
    assert run.error is None


@pytest.mark.asyncio
async def test_the_salvage_reaches_the_next_leg_through_the_checkpoint() -> None:
    """The payoff: the production writer only carries progress that rides ``output``.

    Asserted against the real ``CompactingCheckpointWriter``, because the defect was
    never in the loop alone; it was that the writer had nothing to append.
    """
    script = [_tool_step("c1"), _tool_step("c2"), _resp(_SALVAGE)]
    loop, _, _ = _make_loop(script)
    token = CancelToken()
    watcher = _box_watcher(token, trip_after_steps=2)
    run = await loop.run("t", cancel_token=token, on_event=watcher)

    task = Task(
        id="task-1",
        owner_id="user-1",
        persona_id="astrid",
        contract=Contract(goal="research the rate"),
        created_at=_NOW,
        updated_at=_NOW,
    )
    checkpoint = CompactingCheckpointWriter().write(
        task=task, prior=None, run=run, leg_id="leg-1", seq=1, now=_NOW
    )

    assert checkpoint.progress_conclusions == (_SALVAGE,), (
        "an empty conclusions tuple is exactly what leg N+1 inherited in production, "
        "and why it repeated leg N's searches instead of continuing"
    )
