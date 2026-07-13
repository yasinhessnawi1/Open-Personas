"""Unit tests for persona_runtime.loop.ConversationLoop (T07).

Two tests here are load-bearing (per the Phase 5 directive):

- ``TestBoundaryPredictionLockstep`` cross-checks the loop's ``_will_compact``
  prediction against the real ``ConversationHistoryManager.manage()`` behaviour
  across K-1 / K / K+1. A divergence means a wasted small-tier call or a dropped
  summary — the safety net for the whole D-05-X bridge.
- ``TestEpisodicWriteBackTiming`` proves a partially-consumed turn (consumer
  stops iterating early) leaves episodic empty (acceptance #10 / D-05-12),
  verified by behaviour, not by reasoning about suspension semantics.
"""

# ruff: noqa: SLF001 — tests cross-check the loop's private _will_compact predicate.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.backends.types import TokenUsage
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.skills import SkillSpec
from persona.schema.tools import ToolCall, ToolResult
from persona.skills import SkillInjector, SkillScanner, make_use_skill_tool
from persona.skills._tokens import count_tokens
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop, _aggregate_round_usage
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


_code_exec_calls: list[str] = []


@tool(name="code_execution", description="Run Python code.")
async def _code_execution_tool(code: str) -> ToolResult:
    # Records each real dispatch so a test can prove a TRUNCATED call never
    # reaches the tool (no empty-args dispatch). ``code`` is required, mirroring
    # the real sandbox tool whose validator returns "Field required (at code)".
    _code_exec_calls.append(code)
    return ToolResult(tool_name="code_execution", content="ran", is_error=False)


def _make_loop(
    backend: ScriptedBackend,
    *,
    stores: dict[str, FakeStore] | None = None,
    scanned_skills: list[SkillSpec] | None = None,
    extra_tools: list[object] | None = None,
    max_tool_rounds: int = 5,
    graph_retrieval: object | None = None,
) -> tuple[ConversationLoop, dict[str, FakeStore], MemoryTurnLogWriter]:
    stores = stores or {
        "identity": FakeStore(),
        "self_facts": FakeStore(),
        "worldview": FakeStore(),
        "episodic": FakeStore(),
    }
    tools = list(extra_tools or [_echo_tool])
    toolbox = Toolbox(tools, allow_list=None)  # type: ignore[arg-type]
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    # Force every tier to resolve to our scripted backend.
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]
    writer = MemoryTurnLogWriter()
    loop = ConversationLoop(
        persona=_persona(),
        stores=stores,  # type: ignore[arg-type]
        toolbox=toolbox,
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=scanned_skills or [],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        turn_log_writer=writer,
        max_tool_rounds=max_tool_rounds,
        graph_retrieval=graph_retrieval,  # type: ignore[arg-type]
    )
    return loop, stores, writer


def _conv(turns: int = 0) -> Conversation:
    msgs = [
        ConversationMessage(
            role="user" if i % 2 == 0 else "assistant",
            content=f"turn {i}",
            created_at=datetime.now(UTC),
        )
        for i in range(turns)
    ]
    return Conversation(conversation_id="c1", persona_id="astrid", messages=msgs)


# ----- tests ---------------------------------------------------------------


class TestPlainTurn:
    @pytest.mark.asyncio
    async def test_yields_final_chunk_and_writes_episodic(self) -> None:
        backend = ScriptedBackend([ScriptedRound(text="Hello, I'm Astrid.")])
        loop, stores, writer = _make_loop(backend)
        conv = _conv(0)

        chunks = [c async for c in loop.turn(conv, "hi")]

        assert chunks[-1].is_final is True
        accumulated = "".join(c.delta for c in chunks)
        assert "Hello, I'm Astrid." in accumulated
        # Episodic written exactly once; conversation grew by 2 (user+assistant).
        assert len(stores["episodic"].writes) == 1
        assert conv.turn_count == 2
        assert len(writer.logs) == 1
        assert writer.logs[0].tier_used == "frontier"  # first turn -> frontier

    @pytest.mark.asyncio
    async def test_streams_text_delta_by_delta(self) -> None:
        # The model emits its reply in pieces; the loop must yield each as its own
        # chunk (acceptance §6 #2 — streams char-by-char), not collapse the whole
        # response into a single buffered chunk.
        backend = ScriptedBackend([ScriptedRound(text_deltas=["Hel", "lo, ", "Astrid", " here."])])
        loop, _stores, _writer = _make_loop(backend)

        chunks = [c async for c in loop.turn(_conv(0), "hi")]

        content = [c for c in chunks if c.delta and not c.is_final]
        assert len(content) >= 4, "expected delta-by-delta chunks, not one buffered chunk"
        assert "".join(c.delta for c in chunks) == "Hello, Astrid here."
        assert chunks[-1].is_final is True


class TestToolCallLoop:
    @pytest.mark.asyncio
    async def test_dispatches_tool_then_incorporates_result(self) -> None:
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="echo", tool_args={"message": "ping"}),
                ScriptedRound(text="The tool said: echoed: ping"),
            ]
        )
        loop, stores, writer = _make_loop(backend)
        conv = _conv(2)  # not first turn

        chunks = [c async for c in loop.turn(conv, "use the echo tool")]
        accumulated = "".join(c.delta for c in chunks)

        assert "The tool said: echoed: ping" in accumulated
        assert chunks[-1].is_final is True
        assert writer.logs[0].tool_calls == 1
        # Two chat_stream calls: tool round + final text round.
        assert backend.chat_stream_calls == 2

    @pytest.mark.asyncio
    async def test_hallucinated_tool_name_recovers_instead_of_crashing(self) -> None:
        # Spec-11 soak finding (T03): the model emits a tool call whose name is
        # not in the allow-list (a hallucination — or an empty name from a
        # malformed call). ``toolbox.dispatch`` raises ``ToolNotAllowedError``;
        # the loop must catch it, feed an is_error result back, and let the turn
        # FINISH — not let the error escape the generator and crash the SSE
        # mid-stream ("response already started"). Mirrors the agentic loop's
        # ``_dispatch`` (one tool-failure discipline across both loops).
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="ghost_tool", tool_args={"x": "y"}, call_id="c0"),
                ScriptedRound(text="That tool doesn't exist — here is a direct answer."),
            ]
        )
        loop, _stores, writer = _make_loop(backend)

        chunks = [c async for c in loop.turn(_conv(0), "do the thing")]  # must NOT raise

        accumulated = "".join(c.delta for c in chunks)
        assert "direct answer" in accumulated
        assert chunks[-1].is_final is True
        # the bad call was dispatched once (and recovered), then the model re-answered
        assert writer.logs[0].tool_calls == 1
        assert backend.chat_stream_calls == 2


class TestTruncatedToolCallStreaming:
    """Streaming parse: a tool call whose ``arguments`` JSON was truncated.

    The model wrote a long ``code`` payload that exceeded the response budget,
    so the streamed ``arguments`` fragments never assembled into valid JSON. The
    loop must NOT dispatch ``code_execution`` with empty args (which yields the
    cryptic "Field required" and an identical-retry loop); it must feed back
    actionable "your call was cut off — shorten it" guidance so the model adapts.
    """

    def test_build_call_marks_truncated_on_invalid_json(self) -> None:
        # Truncated mid-object: opening brace + an unterminated string.
        call = ConversationLoop._build_call("c0", "code_execution", '{"code": "print(1')
        assert call.truncated is True
        assert call.args == {}

    def test_build_call_wellformed_not_truncated(self) -> None:
        # Regression: complete arguments parse and dispatch normally.
        call = ConversationLoop._build_call("c0", "code_execution", '{"code": "print(1)"}')
        assert call.truncated is False
        assert call.args == {"code": "print(1)"}

    @pytest.mark.asyncio
    async def test_truncated_call_not_dispatched_empty_and_surfaces_guidance(self) -> None:
        _code_exec_calls.clear()
        # Round 1: a code_execution call whose arguments JSON is truncated.
        # Round 2: the model (having seen the guidance) answers directly.
        backend = ScriptedBackend(
            [
                ScriptedRound(
                    tool_name="code_execution",
                    raw_arguments='{"code": "import pandas as pd\\nfor i in range(100',
                    call_id="c0",
                ),
                ScriptedRound(text="Understood — I'll split that into smaller chunks."),
            ]
        )
        loop, _stores, writer = _make_loop(backend, extra_tools=[_code_execution_tool])

        chunks = [c async for c in loop.turn(_conv(0), "make a styled PDF")]  # must NOT raise

        accumulated = "".join(c.delta for c in chunks)
        assert chunks[-1].is_final is True
        assert "smaller chunks" in accumulated
        # The truncated call was NEVER dispatched with empty args.
        assert _code_exec_calls == []
        assert backend.chat_stream_calls == 2

    @pytest.mark.asyncio
    async def test_dispatch_truncated_returns_actionable_message(self) -> None:
        loop, _stores, _writer = _make_loop(ScriptedBackend([]), extra_tools=[_code_execution_tool])
        call = ToolCall(name="code_execution", args={}, call_id="c0", truncated=True)

        result = await loop._dispatch(call)

        assert result.is_error is True
        # Actionable text — NOT the cryptic "Field required".
        assert "cut off" in result.content
        assert "code_execution" in result.content
        assert "Field required" not in result.content


class TestMaxToolRoundsCap:
    @pytest.mark.asyncio
    async def test_cap_fires_nudge_and_one_final_generation(self) -> None:
        # cap=2: two tool rounds get dispatched (rounds 0,1), then the third
        # generation hits the cap (rounds==2) and the nudge path runs one final
        # generation, which lands on the scripted text round.
        rounds = [
            ScriptedRound(tool_name="echo", tool_args={"message": "x"}, call_id="c0"),
            ScriptedRound(tool_name="echo", tool_args={"message": "y"}, call_id="c1"),
            ScriptedRound(tool_name="echo", tool_args={"message": "z"}, call_id="c2"),
            ScriptedRound(text="best-effort answer"),
        ]
        backend = ScriptedBackend(rounds)
        loop, stores, writer = _make_loop(backend, max_tool_rounds=2)
        conv = _conv(2)

        chunks = [c async for c in loop.turn(conv, "loop forever")]
        accumulated = "".join(c.delta for c in chunks)

        assert chunks[-1].is_final is True
        assert "best-effort answer" in accumulated
        # 2 tool rounds dispatched (the 3rd hit the cap; not dispatched).
        assert writer.logs[0].tool_calls == 2
        # One round == one re-generation (D-05-11): 2 dispatched rounds + the
        # cap-detection generation + the final tool-free generation = 4 streams.
        assert backend.chat_stream_calls == 4

    @pytest.mark.asyncio
    async def test_round_counter_counts_rounds_not_individual_calls(self) -> None:
        # Regression for the round-vs-call semantics (D-05-11): the counter must
        # increment once per round, not once per tool call. With one call per
        # round and cap=1, exactly 1 round is dispatched before the cap fires.
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="echo", tool_args={"message": "a"}, call_id="c0"),
                ScriptedRound(tool_name="echo", tool_args={"message": "b"}, call_id="c1"),
                ScriptedRound(text="final"),
            ]
        )
        loop, _stores, writer = _make_loop(backend, max_tool_rounds=1)
        conv = _conv(2)
        _ = [c async for c in loop.turn(conv, "go")]
        # cap=1: round 0 dispatched (rounds->1); round 1 generation is at_cap ->
        # nudge + final. Exactly 1 tool call dispatched.
        assert writer.logs[0].tool_calls == 1


class TestEmptyFinalRoundFallback:
    """A tool-heavy turn whose final round produces no text must still persist
    the visible text the user actually saw (the ``content=''`` "vanishes on
    refresh" bug). The persisted assistant message must NEVER be blank when any
    text streamed this turn.
    """

    @pytest.mark.asyncio
    async def test_empty_final_round_falls_back_to_accumulated_text(self) -> None:
        # Round 0 streams substantial narration to the user AND calls a tool;
        # round 1 (the post-tool generation) returns NO text. On current code the
        # last round's text (``""``) is what gets persisted → blank bubble.
        backend = ScriptedBackend(
            [
                ScriptedRound(
                    text="Here is the detailed answer the user actually saw.",
                    tool_name="echo",
                    tool_args={"message": "ping"},
                    call_id="c0",
                ),
                ScriptedRound(text=""),  # empty final round (no synthesized answer)
            ]
        )
        loop, stores, writer = _make_loop(backend)
        conv = _conv(2)

        chunks = [c async for c in loop.turn(conv, "do the thing")]
        accumulated = "".join(c.delta for c in chunks)
        assert "Here is the detailed answer" in accumulated

        # The persisted assistant message must equal the visible text, not ''.
        assistant_msgs = [m for m in conv.messages if m.role == "assistant"]
        assert assistant_msgs, "an assistant message must be appended"
        assert assistant_msgs[-1].content.strip(), "assistant content must not be blank"
        assert "Here is the detailed answer the user actually saw." in assistant_msgs[-1].content

        # The episodic write-back must carry the same visible text.
        episodic_chunk = stores["episodic"].writes[-1][0]
        assert "Here is the detailed answer the user actually saw." in episodic_chunk.text
        assert writer.logs, "a TurnLog must be written"

    @pytest.mark.asyncio
    async def test_at_cap_empty_final_generation_falls_back(self) -> None:
        # cap=1: round 0 streams narration + a tool call (dispatched, rounds->1);
        # round 1 is at_cap → nudge + ONE final generation that returns empty.
        # The accumulated narration must survive write-back.
        backend = ScriptedBackend(
            [
                ScriptedRound(
                    text="Searching the statute for the relevant clause now.",
                    tool_name="echo",
                    tool_args={"message": "x"},
                    call_id="c0",
                ),
                ScriptedRound(tool_name="echo", tool_args={"message": "y"}, call_id="c1"),
                ScriptedRound(text=""),  # forced post-cap final generation: empty
            ]
        )
        loop, stores, _writer = _make_loop(backend, max_tool_rounds=1)
        conv = _conv(2)

        _ = [c async for c in loop.turn(conv, "go")]

        assistant_msgs = [m for m in conv.messages if m.role == "assistant"]
        assert assistant_msgs[-1].content.strip()
        assert "Searching the statute" in assistant_msgs[-1].content
        episodic_chunk = stores["episodic"].writes[-1][0]
        assert "Searching the statute" in episodic_chunk.text

    @pytest.mark.asyncio
    async def test_normal_turn_persists_final_round_text_not_concatenation(self) -> None:
        # Regression guard: a normal agentic turn ends with a synthesized answer.
        # The persisted text must be the FINAL round's answer, NOT every round's
        # "let me search…" narration concatenated.
        backend = ScriptedBackend(
            [
                ScriptedRound(
                    text="Let me look that up.",
                    tool_name="echo",
                    tool_args={"message": "ping"},
                    call_id="c0",
                ),
                ScriptedRound(text="The husleieloven says X."),
            ]
        )
        loop, _stores, _writer = _make_loop(backend)
        conv = _conv(2)

        _ = [c async for c in loop.turn(conv, "what does the law say")]

        assistant_msgs = [m for m in conv.messages if m.role == "assistant"]
        assert assistant_msgs[-1].content == "The husleieloven says X."

    @pytest.mark.asyncio
    async def test_truly_textless_turn_persists_honest_marker(self) -> None:
        # A turn that produced NO visible text at all (only a tool call, then an
        # empty final) must not persist a blank bubble — it persists a short
        # honest marker instead.
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="echo", tool_args={"message": "x"}, call_id="c0"),
                ScriptedRound(text=""),  # empty final, no narration anywhere
            ]
        )
        loop, _stores, _writer = _make_loop(backend)
        conv = _conv(2)

        _ = [c async for c in loop.turn(conv, "go")]

        assistant_msgs = [m for m in conv.messages if m.role == "assistant"]
        assert assistant_msgs[-1].content.strip(), "must not persist a blank bubble"


class TestUseSkillIntercept:
    @pytest.mark.asyncio
    async def test_use_skill_injects_and_records_skill_used(self) -> None:
        skill = SkillSpec(
            name="web_research",
            description="Research the web.",
            path=__import__("pathlib").Path("/tmp/web_research/SKILL.md"),
            content="Do careful web research, cite sources.",
            content_token_count=count_tokens("Do careful web research, cite sources."),
        )
        use_skill = make_use_skill_tool([skill])
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "web_research"}),
                ScriptedRound(text="Now I will research carefully."),
            ]
        )
        loop, stores, writer = _make_loop(backend, scanned_skills=[skill], extra_tools=[use_skill])
        conv = _conv(2)

        chunks = [c async for c in loop.turn(conv, "please research X")]
        accumulated = "".join(c.delta for c in chunks)

        assert chunks[-1].is_final is True
        assert "Now I will research carefully." in accumulated
        assert writer.logs[0].skill_used == "web_research"

    @staticmethod
    def _skill(name: str) -> SkillSpec:
        body = f"Instructions for {name}."
        return SkillSpec(
            name=name,
            description=f"{name} skill.",
            path=__import__("pathlib").Path(f"/tmp/{name}/SKILL.md"),
            content=body,
            content_token_count=count_tokens(body),
        )

    @pytest.mark.asyncio
    async def test_composition_chain_of_three_completes(self) -> None:
        # Spec 24 (D-24-4): a depth-3 chain (a→b→c) all activate within one turn.
        skills = [self._skill("a"), self._skill("b"), self._skill("c")]
        use_skill = make_use_skill_tool(skills)
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "a"}),
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "b"}),
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "c"}),
                ScriptedRound(text="Done composing."),
            ]
        )
        loop, _stores, writer = _make_loop(backend, scanned_skills=skills, extra_tools=[use_skill])
        chunks = [c async for c in loop.turn(_conv(2), "do a chain")]
        assert chunks[-1].is_final is True
        # First skill recorded; the turn completed without a crash.
        assert writer.logs[0].skill_used == "a"

    @pytest.mark.asyncio
    async def test_cycle_does_not_crash_the_turn(self) -> None:
        # a→b→a: the cycle is caught at the intercept and surfaced as a system
        # message; the turn proceeds rather than raising SkillCycleError.
        skills = [self._skill("a"), self._skill("b")]
        use_skill = make_use_skill_tool(skills)
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "a"}),
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "b"}),
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "a"}),
                ScriptedRound(text="Recovered from the cycle."),
            ]
        )
        loop, _stores, writer = _make_loop(backend, scanned_skills=skills, extra_tools=[use_skill])
        chunks = [c async for c in loop.turn(_conv(2), "cycle me")]
        assert chunks[-1].is_final is True
        assert "Recovered from the cycle." in "".join(c.delta for c in chunks)

    @pytest.mark.asyncio
    async def test_depth_cap_does_not_crash_the_turn(self) -> None:
        # a→b→c→d: the 4th exceeds the depth cap; caught, turn proceeds.
        skills = [self._skill(n) for n in ("a", "b", "c", "d")]
        use_skill = make_use_skill_tool(skills)
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "a"}),
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "b"}),
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "c"}),
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "d"}),
                ScriptedRound(text="Stopped at the cap."),
            ]
        )
        loop, _stores, writer = _make_loop(
            backend, scanned_skills=skills, extra_tools=[use_skill], max_tool_rounds=6
        )
        chunks = [c async for c in loop.turn(_conv(2), "go deep")]
        assert chunks[-1].is_final is True
        assert writer.logs[0].skill_used == "a"

    @pytest.mark.asyncio
    async def test_turnlog_records_full_skill_invocation_chain(self) -> None:
        # Spec 24 (D-24-10): skills_invoked carries full records (name + params).
        skills = [self._skill("a"), self._skill("b")]
        use_skill = make_use_skill_tool(skills)
        backend = ScriptedBackend(
            [
                ScriptedRound(
                    tool_name="use_skill",
                    tool_args={"skill_name": "a", "parameters": {"k": "v"}},
                ),
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "b"}),
                ScriptedRound(text="Composed."),
            ]
        )
        loop, _stores, writer = _make_loop(backend, scanned_skills=skills, extra_tools=[use_skill])
        [c async for c in loop.turn(_conv(2), "chain")]
        invoked = writer.logs[0].skills_invoked
        assert [r.name for r in invoked] == ["a", "b"]
        assert invoked[0].parameters == {"k": "v"}
        assert invoked[1].parameters is None
        assert all(r.content_tokens > 0 for r in invoked)
        assert writer.logs[0].skill_budget_exceeded is False

    @pytest.mark.asyncio
    async def test_turnlog_flags_skill_budget_exceeded(self) -> None:
        # A composed skill whose content overflows the remaining shared budget
        # is skipped whole and the flag is set (D-24-X-budget-exhaustion-policy).
        small = self._skill("a")
        big = SkillSpec(
            name="big",
            description="huge skill.",
            path=__import__("pathlib").Path("/tmp/big/SKILL.md"),
            content="x",
            content_token_count=5000,  # exceeds the 2000-token shared budget
        )
        use_skill = make_use_skill_tool([small, big])
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "a"}),
                ScriptedRound(tool_name="use_skill", tool_args={"skill_name": "big"}),
                ScriptedRound(text="Budget hit."),
            ]
        )
        loop, _stores, writer = _make_loop(
            backend, scanned_skills=[small, big], extra_tools=[use_skill]
        )
        [c async for c in loop.turn(_conv(2), "overflow")]
        assert writer.logs[0].skill_budget_exceeded is True
        # Only the first skill was actually injected/recorded.
        assert [r.name for r in writer.logs[0].skills_invoked] == ["a"]


class TestEpisodicWriteBackTiming:
    """Acceptance #10 / D-05-12: a partially-consumed turn writes NOTHING."""

    @pytest.mark.asyncio
    async def test_early_consumer_exit_skips_episodic_and_turnlog(self) -> None:
        # Two rounds so there's a tool-call chunk to consume before the end.
        backend = ScriptedBackend(
            [
                ScriptedRound(tool_name="echo", tool_args={"message": "ping"}),
                ScriptedRound(text="final text that the consumer never reaches"),
            ]
        )
        loop, stores, writer = _make_loop(backend)
        conv = _conv(2)

        # Consume only the FIRST chunk, then break (simulating the user
        # navigating away mid-stream). The async generator suspends; the
        # post-loop write-back never runs.
        gen = loop.turn(conv, "start")
        first = await gen.__anext__()
        await gen.aclose()  # close the suspended generator

        assert first is not None
        assert stores["episodic"].writes == []  # NOTHING written
        assert writer.logs == []  # no TurnLog either
        assert conv.turn_count == 2  # conversation NOT appended to

    @pytest.mark.asyncio
    async def test_fully_consumed_turn_writes_exactly_once(self) -> None:
        backend = ScriptedBackend([ScriptedRound(text="done")])
        loop, stores, writer = _make_loop(backend)
        conv = _conv(0)
        _ = [c async for c in loop.turn(conv, "go")]
        assert len(stores["episodic"].writes) == 1
        assert len(writer.logs) == 1


class TestBoundaryPredictionLockstep:
    """D-05-X safety net: loop._will_compact MUST match manage()'s real decision.

    Tested across K-1 / K / K+1 for the real compact_every/keep_recent. A
    divergence means a wasted small-tier summary call or a dropped summary.
    """

    @pytest.mark.parametrize(("compact_every", "keep_recent"), [(10, 5), (6, 2), (4, 1)])
    @pytest.mark.parametrize("delta", [-1, 0, 1, 5])
    def test_prediction_matches_manage(
        self, compact_every: int, keep_recent: int, delta: int
    ) -> None:
        backend = ScriptedBackend([ScriptedRound(text="x")])
        loop, _stores, _writer = _make_loop(backend)
        # Override the loop's manager to the parametrised config.
        manager = ConversationHistoryManager(compact_every=compact_every, keep_recent=keep_recent)
        loop._history = manager  # type: ignore[assignment]

        turns = compact_every + delta
        if turns < 0:
            return
        conv = _conv(turns)

        predicted = loop._will_compact(conv)

        # Ground truth: does manage() actually call the summariser?
        calls: list[int] = []

        def counting(_msgs: list[ConversationMessage]) -> str:
            calls.append(1)
            return "summary"

        manager.manage(conv, summariser=counting)
        actual = len(calls) == 1

        assert predicted == actual, (
            f"prediction {predicted} != actual {actual} "
            f"(compact_every={compact_every}, keep_recent={keep_recent}, turns={turns})"
        )


class TestK4WindowWiring:
    """The K4 gate sees a NON-EMPTY recent window on a real chat turn (Spec K4, T6).

    The load-bearing proof: the gate's recent window must be set by the LOOP as a side
    effect of a real turn — not manually primed. A test that hand-sets the ContextVar
    would prove the gate reads it, not that the loop publishes it (the thing that ships
    broken). So this drives ``loop.turn(...)`` end to end and asserts the gate observed
    the prior conversation, fed through the real ``recent_window_provider``.
    """

    @pytest.mark.asyncio
    async def test_real_turn_publishes_the_recent_window_to_the_gate(self) -> None:
        from persona.graph.config import GraphSettings
        from persona.graph.fusion import HybridResult  # noqa: TC002 — runtime ref in the double
        from persona_runtime.graph_selection import GatingContext, make_graph_retrieval
        from persona_runtime.graph_window import get_recent_window

        seen: list[tuple[str, ...]] = []

        def allowlist_provider(ctx: GatingContext) -> set[str] | None:
            seen.append(ctx.recent_messages)
            return None

        class _EmptyRetriever:
            def retrieve(
                self,
                owner_id: str,  # noqa: ARG002 — contract
                query: str,  # noqa: ARG002 — contract
                *,
                allowlist: set[str] | None = None,  # noqa: ARG002 — contract
                top_k: int | None = None,  # noqa: ARG002 — contract
            ) -> list[HybridResult]:
                return []

        # The REAL composition: recent_window_provider is the actual ContextVar reader,
        # so the only way the gate sees a window is if the loop set it this turn.
        graph_retrieval = make_graph_retrieval(
            retriever=_EmptyRetriever(),
            owner_provider=lambda: "astrid",
            settings=GraphSettings(),
            allowlist_provider=allowlist_provider,
            recent_window_provider=get_recent_window,
        )
        backend = ScriptedBackend([ScriptedRound(text="Take care.")])
        loop, _, _ = _make_loop(backend, graph_retrieval=graph_retrieval)

        # A conversation where a PRIOR turn opened a sensitive topic; the new message is
        # a bland follow-up that does NOT re-name it (the uncanny-re-closing scenario).
        conv = Conversation(
            conversation_id="c1",
            persona_id="astrid",
            messages=[
                ConversationMessage(
                    role="user",
                    content="i keep having panic attacks lately",
                    created_at=datetime.now(UTC),
                ),
            ],
        )

        async for _ in loop.turn(conv, "and what should i do next"):
            pass

        # The gate ran and saw a NON-EMPTY window carrying the prior topic — published by
        # the loop, not primed by the test.
        assert seen, "the gate never ran (graph retrieval was not invoked on the turn)"
        assert seen[0], "the window was EMPTY — the loop did not publish the recent conversation"
        assert any("panic attacks" in m for m in seen[0])


class TestR1HardBypass:
    """B3: R1-hard out-of-band bypass on the CHAT path (Spec V11, V11-D-5).

    Driven by the REAL trigger — a planted crisis message enters through
    ``loop.turn()`` and must produce an ACTUAL generation short-circuit (the
    model is never called), not a forced verdict.
    """

    @pytest.mark.asyncio
    async def test_acute_crisis_bypasses_generation_and_persists(self) -> None:
        # The scripted model output must NOT be used — a HARD turn bypasses it.
        backend = ScriptedBackend([ScriptedRound(text="MODEL_OUTPUT_MUST_NOT_APPEAR")])
        loop, _stores, _writer = _make_loop(backend)
        conv = _conv(0)

        chunks = [c async for c in loop.turn(conv, "i want to kill myself tonight")]
        accumulated = "".join(c.delta for c in chunks)

        # The deterministic safe completion was emitted, discloses AI-ness, and the
        # model output never appears.
        assert "an AI" in accumulated
        assert "MODEL_OUTPUT_MUST_NOT_APPEAR" not in accumulated
        # The persona / model was taken out of the loop ENTIRELY (the real bypass).
        assert backend.chat_stream_calls == 0
        # The exchange is persisted so history reflects the safe completion.
        assert conv.messages[-2].role == "user"
        assert conv.messages[-1].role == "assistant"
        assert "an AI" in conv.messages[-1].content
        assert chunks[-1].is_final is True

    @pytest.mark.asyncio
    async def test_benign_turn_still_calls_the_model(self) -> None:
        # Non-vacuity control: a benign message does NOT bypass — the model runs,
        # so the bypass fires on the real trigger only (the transition, not forced).
        backend = ScriptedBackend([ScriptedRound(text="Hello, I'm Astrid.")])
        loop, _stores, _writer = _make_loop(backend)

        chunks = [c async for c in loop.turn(_conv(0), "what are my rights as a tenant?")]

        assert backend.chat_stream_calls == 1
        assert "Hello, I'm Astrid." in "".join(c.delta for c in chunks)


class TestAggregateRoundUsage:
    """M2 review, finding I2 — the pure summing + mixed-round honesty rule.

    Unit-level pins for ``_aggregate_round_usage`` in isolation (no loop
    machinery needed); the full multi-round trigger chain through a REAL
    ``ConversationLoop.turn()`` is pinned separately in
    ``packages/runtime/tests/integration/test_m2_multiround_usage.py``.
    """

    def test_empty_list_returns_no_usage(self) -> None:
        result = _aggregate_round_usage([])

        assert result.usage is None
        assert result.mixed_actuals_dropped is False

    def test_single_round_with_no_actual_sums_to_itself(self) -> None:
        lone = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

        result = _aggregate_round_usage([lone])

        assert result.usage == lone
        assert result.mixed_actuals_dropped is False

    def test_single_round_with_an_actual_sums_to_itself(self) -> None:
        # Byte-identical pin: N=1 must reproduce the lone round's cost_usd
        # exactly (not merely approximately) — the design-frame requirement.
        lone = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.00042)

        result = _aggregate_round_usage([lone])

        assert result.usage is not None
        assert result.usage.cost_usd == 0.00042
        assert result.mixed_actuals_dropped is False

    def test_multiple_rounds_sum_tokens_with_no_actuals_anywhere(self) -> None:
        rounds = [
            TokenUsage(prompt_tokens=100, completion_tokens=40, total_tokens=140),
            TokenUsage(prompt_tokens=220, completion_tokens=55, total_tokens=275),
            TokenUsage(prompt_tokens=50, completion_tokens=25, total_tokens=75),
        ]

        result = _aggregate_round_usage(rounds)

        assert result.usage is not None
        assert result.usage.prompt_tokens == 370
        assert result.usage.completion_tokens == 120
        assert result.usage.total_tokens == 490
        assert result.usage.cost_usd is None
        assert result.mixed_actuals_dropped is False

    def test_multiple_rounds_sum_actuals_when_every_round_reports_one(self) -> None:
        rounds = [
            TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.0001),
            TokenUsage(prompt_tokens=20, completion_tokens=8, total_tokens=28, cost_usd=0.0002),
            TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7, cost_usd=0.00005),
        ]

        result = _aggregate_round_usage(rounds)

        assert result.usage is not None
        assert result.usage.prompt_tokens == 35
        assert result.usage.completion_tokens == 15
        assert result.usage.cost_usd == pytest.approx(0.00035)
        assert result.mixed_actuals_dropped is False

    def test_zero_cost_round_counts_as_a_reported_actual(self) -> None:
        # A ``:free`` OpenRouter route's actual IS 0.0 — it must count as
        # REPORTED (``is not None``), never as "did not report".
        rounds = [
            TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.0),
            TokenUsage(prompt_tokens=20, completion_tokens=8, total_tokens=28, cost_usd=0.0),
        ]

        result = _aggregate_round_usage(rounds)

        assert result.usage is not None
        assert result.usage.cost_usd == 0.0
        assert result.mixed_actuals_dropped is False

    def test_mixed_rounds_drop_the_actual_and_flag_mixed(self) -> None:
        # Round 1 served OpenRouter with a real actual; round 2 fell back
        # mid-turn to a model that reported no cost. The partial actual must
        # be DROPPED (never summed as if the missing round cost 0), and the
        # caller told to log it (mixed_actuals_dropped=True).
        rounds = [
            TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost_usd=0.0001),
            TokenUsage(prompt_tokens=20, completion_tokens=8, total_tokens=28, cost_usd=None),
            TokenUsage(prompt_tokens=7, completion_tokens=3, total_tokens=10, cost_usd=0.0003),
        ]

        result = _aggregate_round_usage(rounds)

        assert result.usage is not None
        # Tokens ALWAYS sum, even when the actual is dropped.
        assert result.usage.prompt_tokens == 37
        assert result.usage.completion_tokens == 16
        # The actual is dropped, not partially summed (0.0004 would be the
        # bug: silently treating the missing round as free).
        assert result.usage.cost_usd is None
        assert result.mixed_actuals_dropped is True
