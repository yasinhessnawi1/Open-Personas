"""The ``task_pickup`` tool: narrow, honest, and fail-closed (Spec W1, T9; D-W1-10).

The tool itself decides almost nothing, and that is the design: it takes an id, hands it to an
injected port, and reports what came back. Everything about WHICH tasks may be picked up lives
behind that port, where the owner and the persona binding actually are. So what is pinned here
is the small surface the model touches: that it cannot be talked into a wider scope, that a
refusal reveals nothing, and that it never claims work resumed when it did not.
"""

from __future__ import annotations

import pytest
from persona.errors import TaskNotFoundError
from persona.tools.builtin.task_pickup import (
    TASK_PICKUP_TOOL_NAME,
    PickupOutcome,
    make_task_pickup_tool,
)


class _Port:
    """A port that records what it was asked and answers however the test wants."""

    def __init__(self, outcome: PickupOutcome | Exception) -> None:
        self._outcome = outcome
        self.asked: list[str] = []

    def pick_up(self, task_id: str) -> PickupOutcome:
        self.asked.append(task_id)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _tool(port: object | None):  # noqa: ANN202 - the tool's own type is internal
    return make_task_pickup_tool(port_provider=lambda: port, persona_id="astrid")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_it_picks_up_and_says_so_in_the_personas_own_words() -> None:
    port = _Port(PickupOutcome(changed=True, goal="brief hacker news"))
    result = await _tool(port).execute(task_id="task_1")

    assert port.asked == ["task_1"]
    assert result.is_error is False
    assert "brief hacker news" in result.content
    assert result.data == {"changed": True, "note": "", "goal": "brief hacker news"}


@pytest.mark.asyncio
async def test_a_no_op_reports_the_seams_own_reason_and_is_not_an_error() -> None:
    """A finished task, a paused owner and a suspended persona are honest no-ops. Calling them
    errors would have the persona apologise for a fault; claiming success would have it lie."""
    port = _Port(PickupOutcome(changed=False, note="This task has already finished."))
    result = await _tool(port).execute(task_id="task_1")

    assert result.is_error is False
    assert result.content == "This task has already finished."
    assert "picked" not in result.content.lower()


@pytest.mark.asyncio
async def test_a_task_outside_this_personas_reach_reads_exactly_like_a_made_up_one() -> None:
    """Another tenant's task, another persona's task and nonsense are one answer, so a persona
    cannot map what its owner has by watching which refusals differ."""
    refused = await _tool(_Port(TaskNotFoundError("nope", context={"id": "task_x"}))).execute(
        task_id="task_x"
    )
    absent = await _tool(_Port(TaskNotFoundError("nope", context={"id": "made_up"}))).execute(
        task_id="made_up"
    )

    assert refused.is_error is True
    assert refused.content == "I don't have a task with id 'task_x'."
    assert absent.content == "I don't have a task with id 'made_up'."


@pytest.mark.asyncio
async def test_off_request_it_fails_closed() -> None:
    """No caller, no port, no pickup: a tool that acts must never act unscoped."""
    result = await _tool(None).execute(task_id="task_1")
    assert result.is_error is True
    assert "No task context" in result.content


@pytest.mark.asyncio
async def test_it_asks_for_the_id_rather_than_guessing() -> None:
    port = _Port(PickupOutcome(changed=True))
    result = await _tool(port).execute(task_id="   ")

    assert result.is_error is True
    assert port.asked == []  # nothing was picked up on a blank reference


@pytest.mark.asyncio
async def test_the_tool_takes_nothing_but_an_id() -> None:
    """The scope is not an argument: there is no goal, no instruction and no persona to pass,
    so no amount of argument can widen what this tool does."""
    tool = _tool(_Port(PickupOutcome(changed=True)))
    assert tool.name == TASK_PICKUP_TOOL_NAME
    assert list(tool.parameters_schema["properties"]) == ["task_id"]
