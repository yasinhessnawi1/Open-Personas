"""The leg gate: a task's category policy meets the toolbox it runs on (Spec W1, T2).

Spec A3 built ``PolicyGatedToolbox`` and named the leg runner as its one injection site; no
production module ever constructed it. These pin the wiring at the seam itself:

- ``LegGate.for_task`` carries the task's own policy and its owner / task / persona identity,
  and its factory builds the gated toolbox over the tools and allow-list it is handed.
- ``RuntimeFactoryLegRunnerBuilder`` with a recorder builds the loop through that factory
  (a spy runtime factory records the substituted class); without a recorder it stays bare;
  with a recorder but no task it fails fast instead of silently running ungated.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.approvals.gating import PolicyGatedToolbox
from persona.errors import GatedActionProposedError
from persona.schema.tools import ToolCall, ToolResult
from persona.tasks import Contract, Task, TaskKind
from persona.tools import CategoryDecision, Toolbox, tool
from persona_api.errors import LegGateMissingTaskError
from persona_api.tasks.gate import LegGate
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder

if TYPE_CHECKING:
    from persona.approvals import ActionProposal
    from persona.tasks import LegBox

_NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)


def _task(kind: TaskKind = TaskKind.AD_HOC) -> Task:
    return Task(
        id="t-gate",
        owner_id="owner-1",
        persona_id="persona-1",
        contract=Contract(goal="list the three newest AI persona repos"),
        kind=kind,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _Recorder:
    """A ProposalRecorder that keeps what the gate recorded."""

    def __init__(self) -> None:
        self.proposals: list[ActionProposal] = []

    def create_proposal(self, proposal: ActionProposal) -> ActionProposal:
        self.proposals.append(proposal)
        return proposal


@tool(name="web_search", description="Search the web.")
async def _web_search(query: str) -> ToolResult:
    return ToolResult(tool_name="web_search", content=f"results for {query}")


# --- LegGate ----------------------------------------------------------------------


def test_the_gate_carries_the_tasks_policy_and_identity() -> None:
    task = _task()
    gate = LegGate.for_task(task, recorder=_Recorder())
    assert gate.policy is task.contract.category_policy
    assert (gate.context.owner_id, gate.context.task_id, gate.context.persona_id) == (
        "owner-1",
        "t-gate",
        "persona-1",
    )
    assert gate.network_enabled is False


def test_the_factory_builds_a_gated_toolbox_over_the_given_tools() -> None:
    gate = LegGate.for_task(_task(), recorder=_Recorder())
    box = gate.toolbox_factory()([_web_search], allow_list=["web_search"])
    assert isinstance(box, PolicyGatedToolbox)
    assert isinstance(box, Toolbox)  # still the loop's Toolbox type (no loop edit)
    assert box.names() == ["web_search"]


@pytest.mark.asyncio
async def test_a_default_policy_gates_an_unmapped_tool_and_records_the_proposal() -> None:
    """The differential the guard test rests on: gated means recorded THEN raised."""
    recorder = _Recorder()
    gate = LegGate.for_task(_task(), recorder=recorder)
    box = gate.toolbox_factory()([_web_search], allow_list=["web_search"])
    call = ToolCall(name="mcp:mail:send", args={"to": "x@example.com"}, call_id="c1")
    assert gate.policy.decide_tool(frozenset()) is CategoryDecision.GATE
    with pytest.raises(GatedActionProposedError):
        await box.dispatch(call)
    assert len(recorder.proposals) == 1
    assert recorder.proposals[0].task_id == "t-gate"


@pytest.mark.asyncio
async def test_a_free_category_dispatches_unchanged_through_the_gate() -> None:
    gate = LegGate.for_task(_task(), recorder=_Recorder())
    box = gate.toolbox_factory()([_web_search], allow_list=["web_search"])
    result = await box.dispatch(ToolCall(name="web_search", args={"query": "q"}, call_id="c2"))
    assert result.is_error is False
    assert "results for q" in result.content


# --- the runner builder ---------------------------------------------------------------


class _SpyFactory:
    """A RuntimeFactory stand-in recording how the leg's loop was built (gate + question park)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def build_agentic_loop(
        self,
        persona_id: str,
        *,
        toolbox_factory: object = None,
        park_on_question: bool = False,
    ) -> object:
        self.calls.append(
            {
                "persona_id": persona_id,
                "toolbox_factory": toolbox_factory,
                "park_on_question": park_on_question,
            }
        )

        class _Loop:
            async def run(self, task: str, **_: object) -> str:
                return task

        return _Loop()


@pytest.fixture
def box() -> LegBox:
    from persona.tasks import LegBox

    return LegBox()


@pytest.mark.asyncio
async def test_with_a_recorder_the_leg_builds_over_the_gated_toolbox(box: LegBox) -> None:
    factory = _SpyFactory()
    builder = RuntimeFactoryLegRunnerBuilder(factory, recorder=_Recorder())  # type: ignore[arg-type]
    runner = builder.build("t-gate", "persona-1", box, task=_task())

    async def _noop(_: object) -> None:
        return None

    from persona_runtime.agentic.run import CancelToken

    await runner.run("go", on_event=_noop, cancel_token=CancelToken())
    substituted = factory.calls[0]["toolbox_factory"]
    assert callable(substituted)
    made = substituted([_web_search], allow_list=["web_search"])
    assert isinstance(made, PolicyGatedToolbox)


@pytest.mark.asyncio
async def test_without_a_recorder_the_leg_stays_bare(box: LegBox) -> None:
    factory = _SpyFactory()
    runner = RuntimeFactoryLegRunnerBuilder(factory).build(  # type: ignore[arg-type]
        "t-gate", "persona-1", box, task=_task()
    )

    async def _noop(_: object) -> None:
        return None

    from persona_runtime.agentic.run import CancelToken

    await runner.run("go", on_event=_noop, cancel_token=CancelToken())
    assert factory.calls[0]["toolbox_factory"] is None


def test_a_recorder_without_a_task_fails_fast_rather_than_running_ungated(box: LegBox) -> None:
    builder = RuntimeFactoryLegRunnerBuilder(_SpyFactory(), recorder=_Recorder())  # type: ignore[arg-type]
    with pytest.raises(LegGateMissingTaskError):
        builder.build("t-gate", "persona-1", box)


def test_a_leg_always_builds_a_loop_that_parks_on_a_question(box: LegBox) -> None:
    """Spec W1 (D-W1-34): in a leg the user is reachable through the task, so a question
    parks it. A loop built without this answers the question on the user's behalf ("proceed
    with your best judgment"), which is how a task used to finish by talking to itself."""
    for recorder in (_Recorder(), None):
        factory = _SpyFactory()
        builder = RuntimeFactoryLegRunnerBuilder(factory, recorder=recorder)  # type: ignore[arg-type]
        runner = builder.build("t-park", "persona-1", box, task=_task())

        async def _noop(_: object) -> None:
            return None

        from persona_runtime.agentic.run import CancelToken

        asyncio.run(runner.run("go", on_event=_noop, cancel_token=CancelToken()))
        assert factory.calls[0]["park_on_question"] is True
