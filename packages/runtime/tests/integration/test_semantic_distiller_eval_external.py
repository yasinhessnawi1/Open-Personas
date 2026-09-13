"""Does the DISTILLER produce a checkpoint a next leg can continue from? (Spec W1, T13)

The A2 external test proves the judge can tell a coherent leg from an amnesiac one. That is
the instrument, not the measurement. D-W1-18 gates the distiller's flag on continuation
QUALITY, and the thing whose quality decides it is the checkpoint this writer produces.

So: for each committed scenario, a leg is staged exactly as the failure modes describe. The
prior checkpoint holds the established conclusion and the standing plan step. The leg's run
surfaces the fresh fact that invalidates that step. The REAL
:class:`SemanticCheckpointWriter` distils it over a REAL small-tier model, the successor's
context is rebuilt from the resulting checkpoint through the production reconstruction, and
the judge scores what the successor would carry: is the settled conclusion still there
(amnesia), and did the plan move off the step the fresh fact killed (ossification)?

``@pytest.mark.external``, skipped in normal CI. Two real backends, deliberately from
different families (self-enhancement bias, D-A2-X-eval-gate): the distiller under
``PERSONA_PROVIDER``/``PERSONA_MODEL``, the judge under ``PERSONA_JUDGE_PROVIDER``/
``PERSONA_JUDGE_MODEL``. The judge's identity is printed with the result, because a gate
result that does not name its judge cannot be reproduced or believed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from _continuation_eval import Scenario, Verdict, gate, load_scenarios, verdict_for
from persona.backends import BackendConfig, load_backend
from persona.schema.tools import ToolCall, ToolResult
from persona.tasks import (
    Contract,
    ScheduledFire,
    Task,
    TaskCheckpoint,
    reconstruct_context,
)
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs.semantic_distiller import (
    DISTILLER_PROMPT_VERSION,
    SemanticCheckpointWriter,
)
from test_continuation_eval_external import _score  # the same judge call the A2 gate uses

if TYPE_CHECKING:
    from persona.backends.protocol import ChatBackend

_SUITE = Path(__file__).resolve().parents[1] / "fixtures" / "continuation_scenarios.yaml"
_NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)

#: Every scenario must come back COHERENT. This gate decides whether a default-OFF flag ships
#: ON, and a distillation that loses a settled conclusion or keeps a dead plan step on one
#: mission in three is not a distillation anyone should turn on.
_COHERENT_THRESHOLD = 1.0

pytestmark = [
    pytest.mark.external,
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("PERSONA_PROVIDER") or not os.environ.get("PERSONA_MODEL"),
        reason="needs a real backend (PERSONA_PROVIDER/PERSONA_MODEL from root .env)",
    ),
]


def _judge_backend() -> ChatBackend:
    """The judge, from a different family than the distiller (self-enhancement bias)."""
    provider = os.environ.get("PERSONA_JUDGE_PROVIDER")
    model = os.environ.get("PERSONA_JUDGE_MODEL")
    if not provider or not model:
        pytest.skip("needs PERSONA_JUDGE_PROVIDER / PERSONA_JUDGE_MODEL (a different family)")
    key = os.environ.get("PERSONA_JUDGE_API_KEY") or os.environ.get("PERSONA_API_KEY")
    return load_backend(BackendConfig(provider=provider, model=model, api_key=key))  # type: ignore[arg-type]


def _task(scenario: Scenario) -> Task:
    return Task(
        id=f"t-{scenario.id}",
        owner_id="u",
        persona_id="p",
        contract=Contract(goal=scenario.contract_goal),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _prior(scenario: Scenario) -> TaskCheckpoint:
    """Where the task stood: the settled conclusion, and the plan the fresh fact will kill."""
    return TaskCheckpoint(
        task_id=f"t-{scenario.id}",
        leg_id=f"t-{scenario.id}:leg:{scenario.injected_leg - 1}",
        checkpoint_seq=scenario.injected_leg - 1,
        progress_conclusions=(scenario.established_conclusion,),
        current_plan=(scenario.invalidated_plan_step,),
        next_step=scenario.invalidated_plan_step,
        updated_at=_NOW,
    )


def _run(scenario: Scenario) -> Run:
    """The leg that surfaced the fresh fact, as a run the distiller reads."""
    call = ToolCall(name="web_search", args={"query": scenario.contract_goal[:60]}, call_id="c1")
    result = ToolResult(
        tool_name="web_search",
        call_id="c1",
        content=scenario.injected_fresh_fact,
        data={"results": [{"title": "benchmark", "url": "https://example.org/b", "snippet": "s"}]},
    )
    return Run(
        persona_id="p",
        task=scenario.contract_goal,
        status=RunStatus.COMPLETED,
        steps=[Step(type=StepType.TOOL_CALL, tool_calls=[call], results=[result], tokens=900)],
        output=scenario.injected_fresh_fact,
        started_at=_NOW,
        finished_at=_NOW,
    )


def _successor_context(scenario: Scenario, checkpoint: TaskCheckpoint) -> str:
    """What the NEXT leg sees, through the production reconstruction. Nothing else."""
    blocks = reconstruct_context(
        contract=_task(scenario).contract,
        trigger=ScheduledFire(schedule_id="s", fire_time=_NOW),
        checkpoint=checkpoint,
    )
    return "\n\n".join(f"[{b.stage.value}]\n{b.content}" for b in blocks)


def _carried_intent(checkpoint: TaskCheckpoint) -> str:
    """The intent the distiller carried forward: the plan and the one next action."""
    lines = [f"NEXT STEP: {checkpoint.next_step or '(none)'}"]
    if checkpoint.current_plan:
        lines.append("PLAN:")
        lines.extend(f"- {step}" for step in checkpoint.current_plan)
    return "\n".join(lines)


async def test_the_distiller_carries_a_continuable_state(capsys: pytest.CaptureFixture) -> None:
    """The T13 gate: every scenario distilled, every successor judged COHERENT."""
    scenarios = load_scenarios(_SUITE)
    distiller_backend = load_backend(BackendConfig.from_env())
    judge = _judge_backend()
    writer = SemanticCheckpointWriter(backend_provider=lambda: distiller_backend)

    verdicts: list[Verdict] = []
    rows: list[str] = []
    for scenario in scenarios:
        checkpoint = await writer.write(
            task=_task(scenario),
            prior=_prior(scenario),
            run=_run(scenario),
            leg_id=f"t-{scenario.id}:leg:{scenario.injected_leg}",
            seq=scenario.injected_leg,
            now=_NOW,
        )
        context = _successor_context(scenario, checkpoint)
        scores = await _score(judge, scenario, context, _carried_intent(checkpoint))
        verdict = verdict_for(scores)
        verdicts.append(verdict)
        rows.append(f"  {scenario.id}: {verdict.value} (total {scores.total}/10)")

    report = gate(verdicts)
    with capsys.disabled():
        print(
            "\ncontinuation eval, distiller in the loop:"
            f"\n  distiller: {os.environ['PERSONA_PROVIDER']}/{os.environ['PERSONA_MODEL']}"
            f"\n  judge:     {os.environ['PERSONA_JUDGE_PROVIDER']}/"
            f"{os.environ['PERSONA_JUDGE_MODEL']}"
            f"\n  prompt:    {DISTILLER_PROMPT_VERSION}"
            + "\n"
            + "\n".join(rows)
            + f"\n  coherent {report.coherent}/{report.total}, amnesia {report.amnesia}, "
            f"ossification {report.ossification}, inconclusive {report.inconclusive}"
        )

    assert report.passes(coherent_threshold=_COHERENT_THRESHOLD), (
        f"the distiller's continuations did not pass the gate: {report}"
    )
