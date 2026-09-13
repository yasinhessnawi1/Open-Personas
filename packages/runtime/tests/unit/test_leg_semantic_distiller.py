"""The model-backed distiller, over a floor that cannot fail (Spec W1, T13).

A task carried findings between legs and never a plan: the deterministic writer cannot
reason, so it writes an empty ``next_step`` and every leg re-plans from the contract. This
writer reads the leg and returns the task's state after it.

What is pinned here is mostly what happens when it does NOT work, because that is the part
production depends on. A timeout, a raise, a refusal, prose instead of JSON, an empty
distillation: every one of them ends with the deterministic checkpoint, written by the same
writer that would have written it before this existed. Plus the one thing the distiller could
newly break: R9-103, where a ``next_step`` that echoes the leg's answer becomes the
successor's instruction and the task loops forever, producing the same paragraph every hour.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.tools import ToolCall, ToolResult
from persona.tasks import Contract, Task, TaskCheckpoint, enforce_checkpoint_budget
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import CompactingCheckpointWriter
from persona_runtime.legs.semantic_distiller import (
    DISTILLER_PROMPT_VERSION,
    SemanticCheckpointWriter,
)

if TYPE_CHECKING:
    from persona.schema.conversation import ConversationMessage

_NOW = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
_OUTPUT = "Qdrant and Milvus both build in under an hour; pgvector's HNSW build takes nine."


def _task() -> Task:
    return Task(
        id="t1",
        owner_id="u",
        persona_id="p",
        contract=Contract(goal="compare vector stores on build time", scope="under 2h builds"),
        created_at=_NOW,
        updated_at=_NOW,
    )


def _run(output: str = _OUTPUT, *, steps: list[Step] | None = None) -> Run:
    return Run(
        persona_id="p",
        task="x",
        status=RunStatus.COMPLETED,
        steps=steps if steps is not None else [_search_step("pgvector hnsw build time")],
        output=output,
        started_at=_NOW,
        finished_at=_NOW,
    )


def _search_step(query: str) -> Step:
    call = ToolCall(name="web_search", args={"query": query}, call_id="c1")
    result = ToolResult(
        tool_name="web_search",
        call_id="c1",
        content="three results",
        data={"results": [{"title": "t", "url": "https://a.no/x", "snippet": "s"}]},
    )
    return Step(type=StepType.TOOL_CALL, tool_calls=[call], results=[result])


class _ScriptedBackend:
    """Answers with canned content, or fails however the test asks it to."""

    def __init__(
        self,
        content: str = "",
        *,
        raises: BaseException | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._content = content
        self._raises = raises
        self._delay_s = delay_s
        self.prompts: list[str] = []

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_kwargs: Any) -> ChatResponse:  # noqa: ANN401
        self.prompts.append("\n".join(str(m.content) for m in messages))
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if self._raises is not None:
            raise self._raises
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


def _distillation(**overrides: object) -> str:
    payload: dict[str, object] = {
        "conclusions": ["pgvector's HNSW build is 9h, outside the 2h window"],
        "lessons": ["the vendor's published build times exclude index warmup"],
        "plan": ["re-rank Qdrant and Milvus on build time", "write up the two finalists"],
        "next_step": "Benchmark Qdrant's build on the 10M-vector set",
        "open_questions": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


def _writer(
    backend: _ScriptedBackend | None, *, token_budget: int = 2000, timeout_s: float = 20.0
) -> SemanticCheckpointWriter:
    return SemanticCheckpointWriter(
        backend_provider=lambda: backend,  # type: ignore[arg-type, return-value]
        token_budget=token_budget,
        timeout_s=timeout_s,
    )


async def _write(
    writer: SemanticCheckpointWriter, *, prior: TaskCheckpoint | None = None, run: Run | None = None
) -> TaskCheckpoint:
    return await writer.write(
        task=_task(),
        prior=prior,
        run=run if run is not None else _run(),
        leg_id="t1:leg:1",
        seq=1,
        now=_NOW,
    )


# --- what it produces when it works -----------------------------------------


@pytest.mark.asyncio
async def test_the_leg_carries_a_plan_forward() -> None:
    """The whole point. The deterministic floor writes an empty plan and an empty next step,
    so every leg re-planned from the contract and the run kept its knowledge but lost its
    intent."""
    checkpoint = await _write(_writer(_ScriptedBackend(_distillation())))

    assert checkpoint.current_plan == (
        "re-rank Qdrant and Milvus on build time",
        "write up the two finalists",
    )
    assert checkpoint.next_step == "Benchmark Qdrant's build on the 10M-vector set"
    assert checkpoint.progress_conclusions == (
        "pgvector's HNSW build is 9h, outside the 2h window",
    )
    assert checkpoint.lessons == ("the vendor's published build times exclude index warmup",)


@pytest.mark.asyncio
async def test_the_distiller_is_shown_the_contract_the_prior_state_and_the_leg() -> None:
    """It writes what the next leg will read, so it has to see what the task is for, what was
    already settled, and what this leg actually did."""
    backend = _ScriptedBackend(_distillation())
    prior = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        progress_conclusions=("Pinecone is managed-only, excluded",),
        current_plan=("benchmark the remaining three",),
        updated_at=_NOW,
    )

    await _write(_writer(backend), prior=prior)

    prompt = backend.prompts[0]
    assert "compare vector stores on build time" in prompt  # the contract
    assert "Pinecone is managed-only, excluded" in prompt  # the prior state
    assert "benchmark the remaining three" in prompt  # the prior plan
    assert "web_search" in prompt  # what the leg did
    assert _OUTPUT in prompt  # and what it produced


@pytest.mark.asyncio
async def test_the_ledgers_are_carried_by_this_writer_too() -> None:
    """A leg's ledger does not depend on which writer ran (D-W1-16)."""
    checkpoint = await _write(_writer(_ScriptedBackend(_distillation())))

    assert checkpoint.queries_run == ("pgvector hnsw build time",)
    assert checkpoint.sources_seen == ("https://a.no/x",)


@pytest.mark.asyncio
async def test_a_wordy_distillation_still_fits_the_budget() -> None:
    """The same compaction floor: a model that writes an essay cannot overflow the store's
    gate, because the gate is what a checkpoint has to pass to be written at all."""
    wordy = _distillation(
        conclusions=[f"a fairly wordy established finding number {n}" for n in range(200)]
    )
    checkpoint = await _write(_writer(_ScriptedBackend(wordy), token_budget=200))

    enforce_checkpoint_budget(checkpoint, token_budget=200)
    assert checkpoint.progress_conclusions[0].startswith("[")  # the oldest folded


@pytest.mark.asyncio
async def test_one_conclusion_too_large_to_fold_falls_back_to_the_floor() -> None:
    """Found by the W1 operator pass, which watched a real leg park on it.

    Folding keeps WHOLE entries (D-A2-1: compact, never truncate mid-thought), so a single
    conclusion longer than the budget survives folding and the store rejects the append. The
    leg had already done the work, and the rejection cost it the whole checkpoint. A
    distillation that cannot fit is not usable, so the floor writes instead: what the floor
    carries is the run's own output, which is bounded by the model's response budget.
    """
    enormous = " ".join(f"a wordy finding number {n}" for n in range(2000))
    run = _run()

    checkpoint = await _write(
        _writer(_ScriptedBackend(_distillation(conclusions=[enormous])), token_budget=200), run=run
    )
    floor = await CompactingCheckpointWriter(token_budget=200).write(
        task=_task(), prior=None, run=run, leg_id="t1:leg:1", seq=1, now=_NOW
    )

    enforce_checkpoint_budget(checkpoint, token_budget=200)  # it fits, which is the point
    assert checkpoint.content_hash == floor.content_hash  # and it is exactly the floor's


# --- the floor: every way it can fail ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "backend"),
    [
        ("the model raised", _ScriptedBackend(raises=RuntimeError("provider is down"))),
        ("prose instead of JSON", _ScriptedBackend("I think the task is going well!")),
        ("malformed JSON", _ScriptedBackend('{"conclusions": [')),
        ("a JSON list, not an object", _ScriptedBackend("[1, 2, 3]")),
        ("an empty answer", _ScriptedBackend("")),
    ],
)
async def test_every_failure_writes_the_deterministic_checkpoint(
    name: str, backend: _ScriptedBackend
) -> None:
    del name
    run = _run()  # one run: the floor stamps its id as the checkpoint's event_log_cursor
    checkpoint = await _write(_writer(backend), run=run)
    floor = await CompactingCheckpointWriter().write(
        task=_task(), prior=None, run=run, leg_id="t1:leg:1", seq=1, now=_NOW
    )

    assert checkpoint.progress_conclusions == floor.progress_conclusions
    assert checkpoint.next_step == ""  # the floor's honest empty, not a guess
    assert checkpoint.content_hash == floor.content_hash  # byte-identical to the floor


@pytest.mark.asyncio
async def test_a_slow_distillation_falls_back_rather_than_holding_the_leg() -> None:
    """The write happens inside the worker's drain margin. A leg that cannot checkpoint
    because a small tier is slow is a leg whose work is lost."""
    slow = _ScriptedBackend(_distillation(), delay_s=0.3)

    checkpoint = await _write(_writer(slow, timeout_s=0.05))

    assert checkpoint.current_plan == ()  # the floor's shape
    assert checkpoint.progress_conclusions == (_OUTPUT,)


@pytest.mark.asyncio
async def test_no_backend_at_all_is_the_floor_not_an_error() -> None:
    """An install with no small tier configured still runs tasks."""
    checkpoint = await _write(_writer(None))

    assert checkpoint.progress_conclusions == (_OUTPUT,)


@pytest.mark.asyncio
async def test_a_distillation_with_no_conclusions_keeps_what_was_known() -> None:
    """An empty conclusions list is not a distillation; writing it through would erase the
    task's findings on one bad call."""
    prior = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        progress_conclusions=("Pinecone is managed-only, excluded",),
        updated_at=_NOW,
    )

    checkpoint = await _write(_writer(_ScriptedBackend(_distillation(conclusions=[]))), prior=prior)

    assert "Pinecone is managed-only, excluded" in checkpoint.progress_conclusions
    assert _OUTPUT in checkpoint.progress_conclusions


@pytest.mark.asyncio
async def test_an_unparseable_answer_keeps_the_users_open_questions() -> None:
    """Delegating is not the same as building from an empty answer, and this is where the
    difference shows: the floor carries the prior checkpoint's open questions forward, while
    a distillation that said nothing would drop them. A question the user was asked, silently
    deleted because a model returned prose, is the kind of loss nobody notices for days."""
    prior = TaskCheckpoint(
        task_id="t1",
        leg_id="t1:leg:0",
        checkpoint_seq=0,
        progress_conclusions=("something established",),
        open_questions=("Which budget should I assume?",),
        updated_at=_NOW,
    )

    checkpoint = await _write(_writer(_ScriptedBackend("I think that went well!")), prior=prior)

    assert checkpoint.open_questions == ("Which budget should I assume?",)


# --- R9-103: next_step is an instruction, never the answer -------------------


@pytest.mark.asyncio
async def test_a_next_step_that_echoes_the_leg_output_is_refused() -> None:
    """R9-103. ``next_step`` is recited to the successor as its instruction, so an answer
    there makes the successor re-derive the same answer and write it back again. Production
    showed a recurring task emitting byte-identical output every hour, paying full price to
    repeat something it already had. The deterministic writer solved it by writing nothing;
    this writer must not reintroduce it."""
    checkpoint = await _write(_writer(_ScriptedBackend(_distillation(next_step=_OUTPUT))))

    assert checkpoint.next_step == ""


@pytest.mark.asyncio
async def test_a_reformatted_echo_is_still_an_echo() -> None:
    """Whitespace and case are not the difference between an instruction and an answer."""
    reformatted = f"  {_OUTPUT.upper()}  "
    checkpoint = await _write(_writer(_ScriptedBackend(_distillation(next_step=reformatted))))

    assert checkpoint.next_step == ""


@pytest.mark.asyncio
async def test_the_answer_with_a_sentence_bolted_on_is_still_an_echo() -> None:
    padded = f"Report the following. {_OUTPUT}"
    checkpoint = await _write(_writer(_ScriptedBackend(_distillation(next_step=padded))))

    assert checkpoint.next_step == ""


@pytest.mark.asyncio
async def test_a_real_next_step_that_mentions_the_findings_survives() -> None:
    """The guard refuses the ANSWER, not every sentence with a word in common. A next step
    that names what was found and says what to do with it is exactly what should carry."""
    step = "Benchmark Qdrant's build on the 10M-vector set and compare it to pgvector's nine hours"
    checkpoint = await _write(_writer(_ScriptedBackend(_distillation(next_step=step))))

    assert checkpoint.next_step == step


@pytest.mark.asyncio
async def test_a_leg_with_no_output_can_still_carry_a_next_step() -> None:
    """A leg that ended with nothing to say (a gate, a boundary stop) has no answer to echo,
    so the guard has nothing to compare against and must not swallow the plan."""
    checkpoint = await _write(_writer(_ScriptedBackend(_distillation())), run=_run(output=""))

    assert checkpoint.next_step == "Benchmark Qdrant's build on the 10M-vector set"


def test_the_prompt_carries_a_version() -> None:
    """An eval result has to name the prompt it judged (the Spec 10 discipline)."""
    assert DISTILLER_PROMPT_VERSION.startswith("w1-distiller-v")
