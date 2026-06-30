"""Unit tests for persona_runtime.agentic.loop.AgenticLoop (T06).

Covers spec §9 acceptance criteria #1–#7 + #9 and the approved episodic-metadata
change (D-06-8). The scripted backend (extended in _fakes.py) drives non-streaming
chat() through a sequence of ChatResponses (plan → tool → … → final).
"""

# ruff: noqa: SLF001 — tests reach into the registry cache to pin the scripted backend.

from __future__ import annotations

from pathlib import Path

import pytest
from _fakes import FakeStore, ScriptedBackend  # type: ignore[import-not-found]
from persona.backends import BackendConfig, ChatResponse
from persona.backends.types import TokenUsage
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.skills import SkillSpec  # noqa: TC002 — used in a fixture signature
from persona.schema.tools import ToolCall, ToolResult
from persona.skills import SkillInjector, count_tokens, make_use_skill_tool
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.agentic.events import RunEvent  # noqa: TC002 — used in a fixture signature
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.agentic.run import CancelToken, RunStatus
from persona_runtime.agentic.step import StepType
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


# ----- fixtures / builders -------------------------------------------------


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="tenancy assistant",
            background="Knows husleieloven.",
            constraints=["Never give binding advice."],
        ),
    )


@tool(name="echo", description="Echo a message back.")
async def _echo_tool(message: str) -> ToolResult:
    return ToolResult(tool_name="echo", content=f"echoed: {message}", is_error=False)


@tool(name="flaky", description="A tool that reports a failure.")
async def _flaky_tool() -> ToolResult:
    return ToolResult(tool_name="flaky", content="upstream 503", is_error=True)


def _resp(content: str = "", *, tool_calls: list[ToolCall] | None = None) -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="claude-sonnet-4-6",
        provider="anthropic",
        latency_ms=1.0,
    )


def _make_loop(
    script: list[ChatResponse],
    *,
    tools: list[object] | None = None,
    allow_list: list[str] | None = None,
    scanned_skills: list[SkillSpec] | None = None,
    max_steps: int = 20,
    force_frontier_tier: bool = False,
) -> tuple[AgenticLoop, dict[str, FakeStore], ScriptedBackend]:
    stores: dict[str, FakeStore] = {
        "identity": FakeStore(),
        "self_facts": FakeStore(),
        "worldview": FakeStore(),
        "episodic": FakeStore(),
    }
    backend = ScriptedBackend([], chat_script=script)
    toolbox = Toolbox(list(tools or [_echo_tool]), allow_list=allow_list)  # type: ignore[arg-type]
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]
    loop = AgenticLoop(
        persona=_persona(),
        stores=stores,  # type: ignore[arg-type]
        toolbox=toolbox,
        skill_injector=SkillInjector(),
        scanned_skills=scanned_skills or [],
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        max_steps=max_steps,
        force_frontier_tier=force_frontier_tier,
    )
    return loop, stores, backend


async def _collect_events(
    loop: AgenticLoop, task: str, **kw: object
) -> tuple[object, list[RunEvent]]:
    events: list[RunEvent] = []

    async def on_event(ev: RunEvent) -> None:
        events.append(ev)

    run = await loop.run(task, on_event=on_event, **kw)  # type: ignore[arg-type]
    return run, events


# ----- tests ---------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_plan_tool_tool_final_completes(self) -> None:
        # Acceptance #1, #2: plan → tool → tool → final.
        script = [
            _resp(tool_calls=[ToolCall(name="echo", args={"message": "search"}, call_id="c1")]),
            _resp(tool_calls=[ToolCall(name="echo", args={"message": "fetch"}, call_id="c2")]),
            _resp("[FINAL] Here is the complaint letter."),
        ]
        loop, stores, _ = _make_loop(script)
        run, events = await _collect_events(loop, "draft a complaint")

        assert run.status is RunStatus.COMPLETED
        assert run.output == "Here is the complaint letter."
        types = [s.type for s in run.steps]
        assert types == [StepType.TOOL_CALL, StepType.TOOL_CALL, StepType.FINAL]
        # Acceptance #9: the event log shape.
        event_types = [e.type for e in events]
        assert event_types[0] == "started"
        assert "tool_calling" in event_types
        assert "tool_result" in event_types
        assert "completed" in event_types
        assert event_types[-1] == "finished"
        # P2: each dispatched tool emits a paired activity_start/activity_end
        # (additive to tool_result, P2-D-3 keep-both), at the real step index.
        assert event_types.count("activity_start") == 2
        assert event_types.count("activity_end") == 2
        starts = [e for e in events if e.type == "activity_start"]
        assert starts[0].data["name"] == "echo"
        assert starts[0].step >= 0
        # P2 memory_recall bridge: a run now emits a recall event per typed store,
        # after `started` (parity with the chat loop).
        recall = [e for e in events if e.type == "memory_recall"]
        assert {e.data["store"] for e in recall} == {
            "identity",
            "self_facts",
            "worldview",
            "episodic",
        }
        assert event_types.index("started") < event_types.index("memory_recall")

    @pytest.mark.asyncio
    async def test_final_step_records_telemetry(self) -> None:
        loop, _, _ = _make_loop([_resp("[FINAL] done")])
        run = await loop.run("t")
        final = run.steps[-1]
        assert final.tier_used == "frontier"  # step 0 → frontier (D-06-6)
        assert final.tokens == 15


class TestErrorRecovery:
    @pytest.mark.asyncio
    async def test_tool_failure_is_fed_back_and_model_recovers(self) -> None:
        # Acceptance #3: a tool reports is_error; the model recovers next step.
        script = [
            _resp(tool_calls=[ToolCall(name="flaky", args={}, call_id="c1")]),
            _resp("[FINAL] proceeded without the flaky tool"),
        ]
        loop, _, _ = _make_loop(script, tools=[_flaky_tool])
        run = await loop.run("t")
        assert run.status is RunStatus.COMPLETED
        assert run.steps[0].results[0].is_error is True
        assert "503" in run.steps[0].results[0].content

    @pytest.mark.asyncio
    async def test_truncated_tool_call_surfaces_shorten_guidance_not_field_required(self) -> None:
        # The non-streaming parse marked the call truncated (provider cut the
        # arguments JSON off mid-payload). The loop must feed back actionable
        # "your call was cut off — shorten it" guidance and NOT dispatch the
        # tool with empty args (which would yield the cryptic "Field required"
        # and an identical-retry loop).
        truncated = ToolCall(name="code_execution", args={}, call_id="c1", truncated=True)
        script = [
            _resp(tool_calls=[truncated]),
            _resp("[FINAL] understood — splitting into smaller code blocks"),
        ]
        loop, _, _ = _make_loop(script)
        run = await loop.run("make a styled PDF")
        result = run.steps[0].results[0]
        assert result.is_error is True
        assert "cut off" in result.content
        assert "code_execution" in result.content
        assert "Field required" not in result.content
        assert run.status is RunStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_hallucinated_tool_feeds_back_available_tools(self) -> None:
        # Acceptance #4: an unknown tool name → "not available. Available tools: ..."
        script = [
            _resp(tool_calls=[ToolCall(name="search_legal_db", args={}, call_id="c1")]),
            _resp("[FINAL] used a real tool instead"),
        ]
        loop, _, _ = _make_loop(script, tools=[_echo_tool], allow_list=["echo"])
        run = await loop.run("t")
        result = run.steps[0].results[0]
        assert result.is_error is True
        assert "not available" in result.content
        assert "echo" in result.content

    @pytest.mark.asyncio
    async def test_same_hallucinated_tool_twice_adds_stronger_instruction(self) -> None:
        # §5.2: same bad name twice in a row → stronger instruction injected.
        bad = ToolCall(name="ghost", args={}, call_id="c")
        script = [
            _resp(tool_calls=[bad]),
            _resp(tool_calls=[ToolCall(name="ghost", args={}, call_id="c2")]),
            _resp("[FINAL] giving up on ghost"),
        ]
        loop, _, backend = _make_loop(script, tools=[_echo_tool], allow_list=["echo"])
        run = await loop.run("t")
        assert run.status is RunStatus.COMPLETED
        # Three model calls happened (the loop kept going past the repeated bad tool).
        assert backend.chat_calls == 3


class TestAskUser:
    @pytest.mark.asyncio
    async def test_ask_user_marker_triggers_callback(self) -> None:
        # Acceptance #5.
        script = [
            _resp("[ASK_USER] Which apartment is affected?"),
            _resp("[FINAL] thanks, drafted for 3B"),
        ]
        loop, _, _ = _make_loop(script)
        answers: list[str] = []

        async def respond(question: str) -> str:
            answers.append(question)
            return "Apartment 3B"

        run = await loop.run("t", user_respond=respond)
        assert run.status is RunStatus.COMPLETED
        assert answers == ["Which apartment is affected?"]
        ask_step = run.steps[0]
        assert ask_step.type is StepType.ASK_USER
        assert ask_step.user_answer == "Apartment 3B"

    @pytest.mark.asyncio
    async def test_ask_user_without_callback_proceeds(self) -> None:
        script = [
            _resp("[ASK_USER] Which apartment?"),
            _resp("[FINAL] proceeded with best judgment"),
        ]
        loop, _, _ = _make_loop(script)
        run = await loop.run("t")  # no user_respond
        assert run.status is RunStatus.COMPLETED
        assert run.steps[0].user_answer is None

    @pytest.mark.asyncio
    async def test_question_heuristic_without_marker(self) -> None:
        # Short tool-call-free response with a "?" → treated as ask-user (no marker).
        script = [
            _resp("What is your landlord's name?"),
            _resp("[FINAL] done"),
        ]
        loop, _, _ = _make_loop(script)

        async def respond(_q: str) -> str:
            return "Mr. Hansen"

        run = await loop.run("t", user_respond=respond)
        assert run.steps[0].type is StepType.ASK_USER


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_before_run_stops_at_first_boundary(self) -> None:
        # Acceptance #6: cancelled at the step boundary; no half-executed step.
        script = [_resp(tool_calls=[ToolCall(name="echo", args={"message": "x"}, call_id="c1")])]
        loop, _, backend = _make_loop(script)
        token = CancelToken()
        token.cancel()
        run, events = await _collect_events(loop, "t", cancel_token=token)
        assert run.status is RunStatus.CANCELLED
        assert run.steps == []  # no step executed
        assert backend.chat_calls == 0  # no model call before the boundary check
        assert "cancelled" in [e.type for e in events]


class TestMaxSteps:
    @pytest.mark.asyncio
    async def test_max_steps_produces_best_effort_summary(self) -> None:
        # Acceptance #7: never emits [FINAL] → max_steps_reached + best-effort summary.
        # Reasoning steps (no marker) loop until the cap.
        script = [_resp("still thinking, no answer yet") for _ in range(10)]
        loop, _, _ = _make_loop(script, max_steps=3)
        run, events = await _collect_events(loop, "t")
        assert run.status is RunStatus.MAX_STEPS_REACHED
        assert run.output  # best-effort summary (from the scripted backend)
        assert len(run.steps) == 3
        assert "max_steps" in [e.type for e in events]

    @pytest.mark.asyncio
    async def test_max_steps_status_is_not_completed(self) -> None:
        # D-06-2: a max-steps outcome must never read as completed.
        script = [_resp("reasoning") for _ in range(5)]
        loop, _, _ = _make_loop(script, max_steps=2)
        run = await loop.run("t")
        assert run.status is not RunStatus.COMPLETED


class TestReasoningStep:
    @pytest.mark.asyncio
    async def test_reasoning_step_does_not_leave_context_on_assistant(self) -> None:
        # Bug A: a text-only step (no tool call, no [FINAL]/[ASK_USER]) must NOT
        # leave the context ending on an assistant message — Anthropic (and any
        # provider without assistant-prefill) rejects that with a 400. The loop
        # appends a user-role continuation nudge after the assistant reasoning so
        # the NEXT chat() call ends with a user message.
        script = [
            _resp("Let me think about this step by step."),  # reasoning, no marker
            _resp("[FINAL] all done"),
        ]
        loop, _, backend = _make_loop(script)
        run = await loop.run("t")

        assert run.status is RunStatus.COMPLETED
        assert run.output == "all done"
        # Two chat() calls happened; the SECOND (after the reasoning step) must
        # have been handed a context ending in a user-role message, never an
        # assistant message.
        assert backend.chat_calls == 2
        second_context = backend.chat_contexts[1]
        assert second_context[-1].role == "user"


class TestForceFrontier:
    @pytest.mark.asyncio
    async def test_force_frontier_uses_frontier_every_step(self) -> None:
        script = [
            _resp(tool_calls=[ToolCall(name="echo", args={"message": "x"}, call_id="c1")]),
            _resp("[FINAL] done"),
        ]
        loop, _, _ = _make_loop(script, force_frontier_tier=True)
        run = await loop.run("t")
        assert all(s.tier_used == "frontier" for s in run.steps)


class TestUseSkillIntercept:
    @pytest.mark.asyncio
    async def test_use_skill_call_injects_skill_content(self) -> None:
        # D-04-10 / D-06-9: use_skill is just a tool call; its result carries
        # data["skill_name"] and the loop injects the skill content.
        skill = SkillSpec(
            name="web_research",
            description="Research the web.",
            path=Path("/tmp/web_research/SKILL.md"),
            content="Do careful web research, cite sources.",
            content_token_count=count_tokens("Do careful web research, cite sources."),
        )
        use_skill = make_use_skill_tool([skill])
        script = [
            _resp(
                tool_calls=[
                    ToolCall(name="use_skill", args={"skill_name": "web_research"}, call_id="c1")
                ]
            ),
            _resp("[FINAL] researched and drafted"),
        ]
        loop, _, _ = _make_loop(script, tools=[use_skill], scanned_skills=[skill])
        run = await loop.run("research X")
        assert run.status is RunStatus.COMPLETED
        assert run.steps[0].type is StepType.TOOL_CALL
        assert run.steps[0].tool_calls[0].name == "use_skill"


class TestEpisodicWriteBack:
    @pytest.mark.asyncio
    async def test_run_summary_chunk_carries_skill_candidate_metadata(self) -> None:
        # D-06-8 / the approved change: source=agentic_run + run_id/task/tools_used/steps/status.
        script = [
            _resp(tool_calls=[ToolCall(name="echo", args={"message": "x"}, call_id="c1")]),
            _resp("[FINAL] the letter"),
        ]
        loop, stores, _ = _make_loop(script)
        run = await loop.run("draft a complaint about mould")

        episodic_writes = stores["episodic"].writes
        assert len(episodic_writes) == 1
        chunk = episodic_writes[0][0]
        md = chunk.metadata
        assert md["source"] == "agentic_run"
        assert md["run_id"] == run.id
        assert md["task"] == "draft a complaint about mould"
        assert md["tools_used"] == "echo"
        assert md["steps"] == "2"
        assert md["status"] == "completed"
        assert chunk.provenance is not None
        assert chunk.provenance.written_by == "agentic.run"

    @pytest.mark.asyncio
    async def test_episodic_written_for_cancelled_run_too(self) -> None:
        # D-06-8: written for every terminal status.
        loop, stores, _ = _make_loop([_resp("[FINAL] x")])
        token = CancelToken()
        token.cancel()
        run = await loop.run("t", cancel_token=token)
        assert run.status is RunStatus.CANCELLED
        assert len(stores["episodic"].writes) == 1
        assert stores["episodic"].writes[0][0].metadata["status"] == "cancelled"


class TestR1HardBypass:
    """B3: R1-hard out-of-band bypass on the AGENTIC path (Spec V11, V11-D-5).

    Driven by the REAL trigger — a planted crisis task enters through
    ``loop.run()`` and must short-circuit to the safe completion with NO steps and
    NO model call, not a forced verdict.
    """

    @pytest.mark.asyncio
    async def test_acute_crisis_bypasses_all_steps(self) -> None:
        # The scripted model output must NOT be used — a HARD task bypasses it.
        loop, _stores, backend = _make_loop([_resp("[FINAL] MODEL_OUTPUT_MUST_NOT_APPEAR")])

        run = await loop.run("i want to kill myself tonight")

        assert backend.chat_calls == 0  # the persona / model is out of the loop
        assert run.steps == []  # no agentic steps ran
        assert run.output is not None
        assert "an AI" in run.output  # the deterministic safe completion, AI disclosed
        assert "MODEL_OUTPUT_MUST_NOT_APPEAR" not in run.output

    @pytest.mark.asyncio
    async def test_benign_task_still_runs_the_model(self) -> None:
        # Non-vacuity control: a benign task does NOT bypass — the model runs.
        loop, _stores, backend = _make_loop([_resp("[FINAL] done")])

        run = await loop.run("draft a tenancy complaint letter")

        assert backend.chat_calls >= 1
        assert run.output is not None
