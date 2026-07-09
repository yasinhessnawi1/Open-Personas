"""Unit tests for persona_voice.model.VoiceModelReplyProducer (spec V5 T4).

The anti-bypass checkpoint (criterion 1, the product-killer line): the prompt the
voice model receives carries the FULL persona conditioning — identity,
constraints, and retrieved typed memory — via the shared ``PromptBuilder``. Plus:
the producer streams spoken text only (never reasoning), and stamps first-token
latency into the tracker + the VoiceLog listener.
"""

# ruff: noqa: ANN401, ARG001, ARG002 — test doubles with intentionally loose signatures.

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ToolCallDelta
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity, RoutingConfig
from persona.schema.tools import PersistedArtifact, ToolCall, ToolResult
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.agentic.events import RunEvent
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.routing.latency import FirstTokenLatencyTracker
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.loop.streaming import Transcript
from persona_voice.model import (
    DeferredArtifact,
    VoiceModelReplyProducer,
    VoiceTurnContext,
    VoiceTurnRecorder,
)
from persona_voice.model.async_lane import AsyncArtifactLane
from persona_voice.model.expressivity import EXPRESSIVITY_MAP, VoiceExpressivity
from persona_voice.turn_taking.heard_words import BargedReply
from persona_voice.turn_taking.orchestrator import ConversationalOrchestrator
from persona_voice.turn_taking.states import ConversationalState


def _chunk(text: str, meta: dict[str, str] | None = None) -> PersonaChunk:
    return PersonaChunk(
        id=f"id-{abs(hash(text)) % 10000}",
        text=text,
        metadata=meta or {},
        created_at=datetime.now(UTC),
    )


class _FakeStore:
    def __init__(
        self,
        *,
        all_chunks: list[PersonaChunk] | None = None,
        query_chunks: list[PersonaChunk] | None = None,
    ) -> None:
        self._all = all_chunks or []
        self._query = query_chunks or []

    def write(self, persona_id: str, chunks: list[PersonaChunk], **kwargs: Any) -> None:
        self._all.extend(chunks)

    def query(self, persona_id: str, query: str, top_k: int, **filters: Any) -> list[PersonaChunk]:
        return list(self._query[:top_k])

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return list(self._all)

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return list(self._all[-limit:][::-1]) if limit > 0 else []

    def delete(self, persona_id: str) -> None:
        return None


class _ScriptedBackend:
    """A streaming ChatBackend double that captures the prompt + yields chunks."""

    provider_name = "anthropic"
    model_name = "test-model"
    supports_native_tools = False
    supports_vision = False

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = chunks
        self.captured_prompt: list[Any] | None = None

    async def chat_stream(
        self,
        messages: list[Any],
        *,
        tools: Any = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: Any = None,
    ) -> AsyncIterator[StreamChunk]:
        self.captured_prompt = messages
        for chunk in self._chunks:
            yield chunk


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="Norwegian tenancy law assistant",
            background="Knows husleieloven.",
            constraints=["Never give binding legal advice."],
        ),
        routing=RoutingConfig(tier_for_generation="frontier"),  # override → no router needed
    )


def _context(
    backend: object,
    *,
    tracker: FirstTokenLatencyTracker | None = None,
    toolbox: object | None = None,
) -> VoiceTurnContext:
    from persona.backends import BackendConfig

    stores = {
        "identity": _FakeStore(all_chunks=[_chunk("I am Astrid, a tenancy assistant.")]),
        "self_facts": _FakeStore(query_chunks=[_chunk("I specialise in tenancy law.")]),
        "worldview": _FakeStore(query_chunks=[_chunk("Tenants have strong protections.")]),
        "episodic": _FakeStore(query_chunks=[_chunk("Last time we discussed mould.")]),
    }
    cfg = BackendConfig(provider="anthropic", model="test-model", api_key=None)  # type: ignore[arg-type]
    registry = TierRegistry({"frontier": TierConfig(name="frontier", backend_config=cfg)})
    registry._cache = {"frontier": backend}  # type: ignore[assignment,dict-item]  # noqa: SLF001
    return VoiceTurnContext(
        persona=_persona(),
        stores=stores,  # type: ignore[arg-type]
        conversation=Conversation(conversation_id="c1", persona_id="astrid", messages=[]),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        latency_tracker=tracker,
        toolbox=toolbox,  # type: ignore[arg-type]
    )


def _final() -> StreamChunk:
    return StreamChunk(
        delta="",
        is_final=True,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


async def _drain(producer: VoiceModelReplyProducer, text: str = "What are my rights?") -> list[str]:
    stream = await producer(Transcript(is_final=True, text=text, confidence=1.0))
    return [tok async for tok in stream]


class TestAntiBypass:
    """Criterion 1 — the model receives the FULL persona conditioning (no bypass)."""

    @pytest.mark.asyncio
    async def test_prompt_carries_identity_constraints_and_memory(self) -> None:
        backend = _ScriptedBackend([StreamChunk(delta="Hello"), _final()])
        producer = VoiceModelReplyProducer(_context(backend))

        await _drain(producer)

        assert backend.captured_prompt is not None
        system = backend.captured_prompt[0].content
        assert "You are Astrid" in system  # identity
        assert "You must NOT:" in system  # constraints enforced
        assert "Never give binding legal advice." in system
        assert "Relevant facts about yourself:" in system  # self_facts retrieved
        assert "Your views:" in system  # worldview retrieved
        assert "From earlier conversations:" in system  # episodic retrieved
        # The transcribed user turn is the last message.
        assert backend.captured_prompt[-1].role == "user"
        assert backend.captured_prompt[-1].content == "What are my rights?"


class TestStreaming:
    @pytest.mark.asyncio
    async def test_yields_reply_tokens_in_order(self) -> None:
        backend = _ScriptedBackend(
            [StreamChunk(delta="Hel"), StreamChunk(delta="lo"), StreamChunk(delta="!"), _final()]
        )
        producer = VoiceModelReplyProducer(_context(backend))
        assert await _drain(producer) == ["Hel", "lo", "!"]

    @pytest.mark.asyncio
    async def test_reasoning_is_never_spoken(self) -> None:
        # A reasoning-only chunk (no delta) must not be yielded to TTS.
        backend = _ScriptedBackend(
            [
                StreamChunk(delta="", reasoning="(thinking hard)"),
                StreamChunk(delta="Answer"),
                _final(),
            ]
        )
        producer = VoiceModelReplyProducer(_context(backend))
        assert await _drain(producer) == ["Answer"]


class TestFirstTokenStamping:
    @pytest.mark.asyncio
    async def test_records_latency_into_tracker(self) -> None:
        tracker = FirstTokenLatencyTracker()
        backend = _ScriptedBackend([StreamChunk(delta="Hi"), _final()])
        producer = VoiceModelReplyProducer(_context(backend, tracker=tracker))

        await _drain(producer)

        assert tracker.sample_count("test-model") == 1

    @pytest.mark.asyncio
    async def test_notifies_first_token_listener_once_with_timestamp(self) -> None:
        stamps: list[datetime] = []
        fixed = datetime(2026, 6, 14, 12, 0, 0, tzinfo=UTC)
        backend = _ScriptedBackend([StreamChunk(delta="a"), StreamChunk(delta="b"), _final()])
        producer = VoiceModelReplyProducer(
            _context(backend), first_token_listener=stamps.append, clock=lambda: fixed
        )

        await _drain(producer)

        assert stamps == [fixed]  # exactly once, on the first token


class _ParkingBackend:
    """A streaming backend that parks after the first token (cancellable shape).

    Mirrors the Spec 02 backend's ``async with provider.stream()`` close-on-cancel:
    the ``finally`` sets ``closed`` exactly as ``__aexit__`` would, so a barge-in
    cancel mid-stream proves the stream closes cleanly (R-V5-4 / criterion 5).
    """

    provider_name = "anthropic"
    model_name = "test-model"
    supports_native_tools = False
    supports_vision = False

    def __init__(self) -> None:
        self.closed = False
        self.emitted: list[str] = []
        self.first_emitted = asyncio.Event()
        self.release = asyncio.Event()

    async def chat_stream(
        self,
        messages: list[Any],
        *,
        tools: Any = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: Any = None,
    ) -> AsyncIterator[StreamChunk]:
        try:
            self.emitted.append("Hel")
            yield StreamChunk(delta="Hel")
            self.first_emitted.set()
            await self.release.wait()  # park mid-stream (cancellable point)
            for token in ("lo", "!"):
                self.emitted.append(token)
                yield StreamChunk(delta=token)
            yield _final()
        finally:
            self.closed = True  # the clean-close the real `async with` guarantees


class TestCancellation:
    """R-V5-4 / criterion 5 — cancel mid-generation stops fast + closes clean."""

    @pytest.mark.asyncio
    async def test_cancel_mid_stream_stops_and_closes_cleanly(self) -> None:
        backend = _ParkingBackend()
        producer = VoiceModelReplyProducer(_context(backend))
        spoken: list[str] = []

        async def run() -> None:
            stream = await producer(Transcript(is_final=True, text="hi", confidence=1.0))
            async for token in stream:
                spoken.append(token)

        task = asyncio.create_task(run())
        await asyncio.wait_for(backend.first_emitted.wait(), timeout=1.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert spoken == ["Hel"]  # only the spoken-so-far prefix
        assert backend.emitted == ["Hel"]  # the remainder was never generated
        assert backend.closed is True  # the provider stream closed cleanly


# ----- T7 voice tools ------------------------------------------------------


class _MultiRoundBackend:
    """A streaming backend that serves a different chunk list per chat_stream call."""

    provider_name = "anthropic"
    model_name = "test-model"
    supports_native_tools = True
    supports_vision = False

    def __init__(self, rounds: list[list[StreamChunk]]) -> None:
        self._rounds = rounds
        self._call = 0

    async def chat_stream(
        self,
        messages: list[Any],
        *,
        tools: Any = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: Any = None,
    ) -> AsyncIterator[StreamChunk]:
        chunks = self._rounds[self._call]
        self._call += 1
        for chunk in chunks:
            yield chunk


def _tool_call_chunk(name: str, args: str) -> StreamChunk:
    return StreamChunk(
        delta="", tool_call_delta=ToolCallDelta(call_id="c1", name_delta=name, arguments_delta=args)
    )


@tool(name="web_search", description="Search the web.")
async def _web_search(query: str) -> ToolResult:
    return ToolResult(tool_name="web_search", content=f"results for {query}")


@tool(name="web_search", description="A slow search.")
async def _slow_web_search(query: str) -> ToolResult:
    await asyncio.sleep(1.0)
    return ToolResult(tool_name="web_search", content="late")


@tool(name="code_execution", description="Run code.")
async def _code_execution(code: str) -> ToolResult:
    msg = "a deferred tool must never run on the live voice path"
    raise RuntimeError(msg)


@tool(name="generate_image", description="Generate an image.")
async def _generate_image(prompt: str) -> ToolResult:
    msg = "image gen must not run inline on the live voice path before the T3 async lane"
    raise RuntimeError(msg)


class TestVoiceToolsConversational:
    """Criterion 8 — a voice-viable tool works conversationally (preamble + result)."""

    @pytest.mark.asyncio
    async def test_voice_viable_tool_narrates_then_returns_result(self) -> None:
        backend = _MultiRoundBackend(
            [
                [_tool_call_chunk("web_search", '{"query": "rights"}'), _final()],
                [StreamChunk(delta="You have strong rights."), _final()],
            ]
        )
        toolbox = Toolbox([_web_search], allow_list=None)  # type: ignore[list-item]
        producer = VoiceModelReplyProducer(_context(backend, toolbox=toolbox))

        out = await _drain(producer)

        assert "Let me look that up." in out  # the first-class preamble (D-V5-5)
        assert "You have strong rights." in out  # the tool ran → re-prompt answer
        # The conversation stays alive: preamble spoken before the result.
        assert out.index("Let me look that up.") < out.index("You have strong rights.")

    @pytest.mark.asyncio
    async def test_deferred_tool_is_acknowledged_not_executed(self) -> None:
        backend = _MultiRoundBackend(
            [[_tool_call_chunk("code_execution", '{"code": "x"}'), _final()]]
        )
        toolbox = Toolbox([_code_execution], allow_list=None)  # type: ignore[list-item]
        captured: list[DeferredArtifact] = []
        producer = VoiceModelReplyProducer(
            _context(backend, toolbox=toolbox), deferred_artifact_listener=captured.append
        )

        # If the deferred tool were dispatched it would raise — reaching here proves
        # it was acknowledged, not executed (D-V5-4).
        out = await _drain(producer)

        assert "I'll prepare that and have it ready for you after our call." in out
        assert len(captured) == 1
        assert captured[0].tool_name == "code_execution"

    @pytest.mark.asyncio
    async def test_async_artifact_tool_submits_to_lane_not_run_inline(self) -> None:
        # T3 (V10-D-3): with the async lane wired, an ASYNC_ARTIFACT call is
        # acknowledged inline ("…on screen…") and handed to the off-turn lane —
        # NEVER dispatched inline (the raising fake proves it does not run on the
        # live path), and never blocking the turn.
        backend = _MultiRoundBackend(
            [[_tool_call_chunk("generate_image", '{"prompt": "a castle"}'), _final()]]
        )
        toolbox = Toolbox([_generate_image], allow_list=None)  # type: ignore[list-item]
        submitted: list[ToolCall] = []
        producer = VoiceModelReplyProducer(
            _context(backend, toolbox=toolbox), async_artifact_listener=submitted.append
        )

        out = await _drain(producer)

        assert any("screen" in tok.lower() for tok in out)  # inline acknowledgement
        assert len(submitted) == 1  # handed to the off-turn lane
        assert submitted[0].name == "generate_image"

    @pytest.mark.asyncio
    async def test_async_artifact_tool_acknowledged_pending_lane(self) -> None:
        # T2 interim (V10-D-1): generate_image is now REACHABLE in voice (it was
        # silently unoffered — V5's deferred set used the dead name
        # "image_generation"). Until T3 wires the async production lane it is
        # acknowledged off-path like a deferred tool, never run inline and never
        # stranding the call (the raising fake proves it is NOT dispatched live).
        backend = _MultiRoundBackend(
            [[_tool_call_chunk("generate_image", '{"prompt": "a castle"}'), _final()]]
        )
        toolbox = Toolbox([_generate_image], allow_list=None)  # type: ignore[list-item]
        captured: list[DeferredArtifact] = []
        producer = VoiceModelReplyProducer(
            _context(backend, toolbox=toolbox), deferred_artifact_listener=captured.append
        )

        out = await _drain(producer)

        assert "I'll prepare that and have it ready for you after our call." in out
        assert len(captured) == 1
        assert captured[0].tool_name == "generate_image"

    @pytest.mark.asyncio
    async def test_voice_tool_overrun_speaks_graceful_overflow(self) -> None:
        backend = _MultiRoundBackend([[_tool_call_chunk("web_search", '{"query": "x"}'), _final()]])
        toolbox = Toolbox([_slow_web_search], allow_list=None)  # type: ignore[list-item]
        producer = VoiceModelReplyProducer(_context(backend, toolbox=toolbox), tool_timeout_s=0.01)

        out = await _drain(producer)

        assert "Let me look that up." in out  # preamble still spoken
        assert "This is taking a moment — I'll follow up on that shortly." in out  # bound hit


class TestVoiceActivitySeam:
    """V10 T1 — voice tool dispatch routes through P2's shared activity seam.

    The voice path must NOT invent a parallel event format (the one thing P2
    forbids): a live voice tool dispatches through the SAME
    ``dispatch_with_activity`` seam chat/runs use, so a call emits a paired
    ``activity_start``/``activity_end`` ``RunEvent`` (the unified "using <X>…"
    vocabulary) when a sink is present — and stays byte-identical (bare dispatch,
    no instrumentation cost) when it is absent (no talk-only regression, #6).
    """

    @pytest.mark.asyncio
    async def test_voice_viable_tool_emits_paired_activity_events(self) -> None:
        backend = _MultiRoundBackend(
            [
                [_tool_call_chunk("web_search", '{"query": "rights"}'), _final()],
                [StreamChunk(delta="You have strong rights."), _final()],
            ]
        )
        toolbox = Toolbox([_web_search], allow_list=None)  # type: ignore[list-item]
        events: list[RunEvent] = []

        async def _sink(event: RunEvent) -> None:
            events.append(event)

        producer = VoiceModelReplyProducer(_context(backend, toolbox=toolbox), on_event=_sink)

        await _drain(producer)

        types = [e.type for e in events]
        assert "activity_start" in types
        assert "activity_end" in types
        start = next(e for e in events if e.type == "activity_start")
        end = next(e for e in events if e.type == "activity_end")
        # The unified contract: kind derived from the tool name, paired by id, ok.
        assert start.data["kind"] == "web"
        assert start.data["name"] == "web_search"
        assert end.data["status"] == "ok"
        assert start.data["activity_id"] == end.data["activity_id"]
        # Start precedes end (the live "using web search…" → resolved lifecycle).
        assert types.index("activity_start") < types.index("activity_end")
        # Run-level voice turn → step=-1 (mirrors the chat turn, never a real step).
        assert start.step == -1
        assert end.step == -1

    @pytest.mark.asyncio
    async def test_inline_artifact_tool_emits_tool_result_for_the_panel(self) -> None:
        # render_diagram is INLINE (VOICE_VIABLE) and produces an artifact; the
        # producer must emit a tool_result frame carrying the artifact so it
        # renders in the FileRendererPanel — the inline analog of the async lane's
        # render-when-ready frame (T4, render_diagram parity).
        art = PersistedArtifact(
            workspace_path="uploads/d.svg",
            mime_type="text/vnd.mermaid",
            size_bytes=10,
            rendered_inline=True,
        )

        @tool(name="render_diagram", description="Render a diagram.")
        async def _render(spec: str) -> ToolResult:  # noqa: ARG001
            return ToolResult(tool_name="render_diagram", content="diagram", artifacts=(art,))

        backend = _MultiRoundBackend(
            [
                [_tool_call_chunk("render_diagram", '{"spec": "graph"}'), _final()],
                [StreamChunk(delta="It's on screen."), _final()],
            ]
        )
        toolbox = Toolbox([_render], allow_list=None)  # type: ignore[list-item]
        events: list[RunEvent] = []

        async def _sink(event: RunEvent) -> None:
            events.append(event)

        producer = VoiceModelReplyProducer(_context(backend, toolbox=toolbox), on_event=_sink)

        await _drain(producer)

        results = [e for e in events if e.type == "tool_result"]
        assert len(results) == 1
        assert results[0].data["artifacts"][0]["workspace_path"] == "uploads/d.svg"

    @pytest.mark.asyncio
    async def test_no_sink_runs_the_tool_without_emitting_events(self) -> None:
        # Default (no sink): byte-identical to today — the tool still runs and its
        # result is incorporated, with no activity instrumentation on the path.
        backend = _MultiRoundBackend(
            [
                [_tool_call_chunk("web_search", '{"query": "rights"}'), _final()],
                [StreamChunk(delta="You have strong rights."), _final()],
            ]
        )
        toolbox = Toolbox([_web_search], allow_list=None)  # type: ignore[list-item]
        producer = VoiceModelReplyProducer(_context(backend, toolbox=toolbox))

        out = await _drain(producer)

        assert "You have strong rights." in out  # the tool ran → re-prompt answer


class TestRetrievalRunsOffTheEventLoop:
    """Voice-event-loop fix: the CPU-bound embedder recall must not block the loop.

    The persona-store ``query`` (which runs the synchronous bge-small ``.encode()``)
    is offloaded via ``asyncio.to_thread`` on the graph-OFF path (the production
    default — the runner wires no graph on the voice ``VoiceTurnContext``), exactly
    as the graph-ON path already did. If it ran inline, LiveKit heartbeats and the
    provider WebSocket handshakes would starve mid-turn — the live incident's root
    cause.
    """

    @pytest.mark.asyncio
    async def test_graph_off_retrieval_runs_in_a_worker_thread(self) -> None:
        import threading

        loop_thread = threading.get_ident()
        query_threads: list[int] = []

        class _ThreadRecordingStore(_FakeStore):
            def query(
                self, persona_id: str, query: str, top_k: int, **filters: Any
            ) -> list[PersonaChunk]:
                query_threads.append(threading.get_ident())
                return super().query(persona_id, query, top_k, **filters)

        ctx = _context(_ScriptedBackend([StreamChunk(delta="ok"), _final()]))
        # Swap the variable stores for thread-recording ones (identity is cached
        # session-constant and read via get_all, not the per-turn embedder query).
        ctx.stores["self_facts"] = _ThreadRecordingStore(
            query_chunks=[_chunk("I specialise in tenancy law.")]
        )
        ctx.stores["worldview"] = _ThreadRecordingStore(
            query_chunks=[_chunk("Tenants have strong protections.")]
        )
        ctx.stores["episodic"] = _ThreadRecordingStore(
            query_chunks=[_chunk("Last time we discussed mould.")]
        )
        producer = VoiceModelReplyProducer(ctx)

        await _drain(producer)

        # The embedder-backed query ran (graph-off path is the production default)
        # and every call happened OFF the event-loop thread.
        assert query_threads, "expected the per-turn persona-store query to run"
        assert all(tid != loop_thread for tid in query_threads)


class TestUnifiedMemoryWiring:
    """T8 — the producer notes the user turn so the recorder can correlate it."""

    @pytest.mark.asyncio
    async def test_producer_notes_user_so_commit_writes_the_pair(self) -> None:
        backend = _ScriptedBackend([StreamChunk(delta="Noted."), _final()])
        ctx = _context(backend)
        recorder = VoiceTurnRecorder(ctx)
        producer = VoiceModelReplyProducer(ctx, turn_recorder=recorder)

        await _drain(producer, text="remember my cat Milo")
        await recorder.on_reply_committed(
            BargedReply(heard_text="Noted.", truncated=False, token_count=1)
        )

        written = ctx.stores["episodic"].get_all("astrid", include_superseded=True)
        assert any("remember my cat Milo" in c.text and "Noted." in c.text for c in written)

    @pytest.mark.asyncio
    async def test_greeting_turn_persists_the_greeting_but_never_the_nudge(self) -> None:
        """R9-001 — driven through the REAL producer path with the runner's REAL
        greeting-turn construction (``_greeting_transcript``, the exact object
        ``begin_greeting`` feeds ``invoke_model_for_turn``): the synthetic flag
        rides producer → recorder → persistence, so the committed turn persists
        exactly ONE transcript row (the assistant greeting) and the nudge text
        never reaches the durable transcript or episodic memory."""
        from persona_voice.agent.runner import _GREETING_NUDGE, _greeting_transcript

        class _RecordingTranscriptWriter:
            def __init__(self) -> None:
                self.turns: list[dict[str, Any]] = []

            def record_turn(
                self, *, user_text: str | None, heard_text: str, truncated: bool, now: datetime
            ) -> None:
                self.turns.append({"user_text": user_text, "heard_text": heard_text})

        backend = _ScriptedBackend([StreamChunk(delta="Hei! So glad you called."), _final()])
        ctx = _context(backend)
        writer = _RecordingTranscriptWriter()
        recorder = VoiceTurnRecorder(ctx, transcript_writer=writer)  # type: ignore[arg-type]
        producer = VoiceModelReplyProducer(ctx, turn_recorder=recorder)

        # Turn 0 exactly as production runs it: the runner's greeting transcript
        # through the producer, then V4's commit hook with the heard greeting.
        greeting = _greeting_transcript()
        stream = await producer(greeting)
        spoken = "".join([tok async for tok in stream])
        await recorder.on_reply_committed(
            BargedReply(heard_text=spoken, truncated=False, token_count=5)
        )

        # The persona's greeting reply persists (cross-device transcript +
        # memory continuity) — as the ONLY row of the turn: no user row.
        assert writer.turns == [{"user_text": None, "heard_text": "Hei! So glad you called."}]
        # The nudge never reaches any durable surface.
        written = ctx.stores["episodic"].get_all("astrid", include_superseded=True)
        assert written
        assert all(_GREETING_NUDGE not in c.text for c in written)
        assert any("Hei! So glad you called." in c.text for c in written)


@tool(name="generate_image", description="Generate an image (succeeds).")
async def _generate_image_ok(prompt: str) -> ToolResult:
    return ToolResult(tool_name="generate_image", content=f"image of {prompt}")


class _NarrationActions:
    """A real TurnActions double — records the model-invocations the orchestrator
    performs (the only faked seam; the state machine + triggers are real)."""

    def __init__(self) -> None:
        self.invoked: list[Transcript] = []

    async def invoke_model_for_turn(self, final_transcript: Transcript) -> None:
        self.invoked.append(final_transcript)

    async def cancel_generation(self) -> None:
        return None

    async def interrupt(self) -> None:
        return None


class TestAsyncArtifactRealChain:
    """T3 cycle 5 (R-3) — the full render-when-ready chain end-to-end through the
    REAL orchestrator edge, with NO hand-forced state: the producer acknowledges
    + submits → the lane produces off-turn + emits the render frame → on_ready is
    the real floor-gated ``notify_artifact_ready`` → it fires the real
    LISTENING→PROCESSING narration turn."""

    @pytest.mark.asyncio
    async def test_producer_to_lane_to_real_orchestrator_narration(self) -> None:
        actions = _NarrationActions()
        orch = ConversationalOrchestrator(actions=actions)  # real machine, idle floor
        assert orch.state is ConversationalState.LISTENING

        events: list[RunEvent] = []

        async def _on_event(ev: RunEvent) -> None:
            events.append(ev)

        toolbox = Toolbox([_generate_image_ok], allow_list=None)  # type: ignore[list-item]
        lane = AsyncArtifactLane(
            toolbox=toolbox, on_ready=orch.notify_artifact_ready, on_event=_on_event
        )

        backend = _MultiRoundBackend(
            [[_tool_call_chunk("generate_image", '{"prompt": "a castle"}'), _final()]]
        )
        producer = VoiceModelReplyProducer(
            _context(backend, toolbox=toolbox), async_artifact_listener=lane.submit
        )

        # The live turn: acknowledged inline + handed to the lane (fast path).
        out = await _drain(producer)
        assert any("screen" in tok.lower() for tok in out)
        # The narration has NOT fired yet — production is still off-turn.
        assert len(actions.invoked) == 0

        # Off-turn production completes → render frame emitted → narration fired
        # through the REAL agent-initiated LISTENING→PROCESSING edge.
        await lane.join()

        types = [e.type for e in events]
        assert "activity_start" in types  # the "creating an image…" badge
        assert "tool_result" in types  # the artifact render frame (panel)
        assert orch.state is ConversationalState.PROCESSING  # real edge, not forced
        assert len(actions.invoked) == 1  # the coalesced narration turn


class TestR1HardBypass:
    """B3: R1-hard out-of-band bypass on the VOICE path (Spec V11, V11-D-5).

    Driven by the REAL trigger — a planted crisis transcript enters through the
    producer and must speak the SHORT spoken safe-completion variant (Group-B
    note 2), with NO model call (the persona is out of the loop).
    """

    @pytest.mark.asyncio
    async def test_acute_crisis_speaks_spoken_variant_without_calling_model(self) -> None:
        from persona_runtime.safety_intercept import safe_completion

        # A delta the model would stream — it must NOT be spoken on a HARD turn.
        backend = _ScriptedBackend([StreamChunk(delta="MODEL_MUST_NOT_SPEAK"), _final()])
        producer = VoiceModelReplyProducer(_context(backend))

        tokens = await _drain(producer, text="i want to kill myself tonight")
        spoken = "".join(tokens)

        # The SHORT spoken variant was emitted (not the chat resource block), AI
        # disclosed, and the model output never appears.
        assert spoken == safe_completion(None).voice_text
        assert "an AI" in spoken
        assert "MODEL_MUST_NOT_SPEAK" not in spoken
        # The persona / model was taken out of the loop entirely (never called).
        assert backend.captured_prompt is None

    @pytest.mark.asyncio
    async def test_benign_transcript_still_calls_the_model(self) -> None:
        # Non-vacuity control: a benign transcript does NOT bypass — the model runs.
        backend = _ScriptedBackend([StreamChunk(delta="Hello"), _final()])
        producer = VoiceModelReplyProducer(_context(backend))

        await _drain(producer, text="what are my rights as a tenant?")

        assert backend.captured_prompt is not None  # the model was called


class TestFeelingTagStrip:
    """N5-A8 — a raw feeling-tag must NEVER reach TTS (criterion 3, voice path).

    Voice is CHAT-mode-gated for tag *emission* (N5-D-5), so tags are rare here — but
    the STRIP filter is the path-independent safety floor: a spoken ``{{#…}}`` is the
    audio leak. Expressivity on voice is V12's job; N5 only guarantees no escape.
    """

    @pytest.mark.asyncio
    async def test_whole_tag_is_stripped_from_speech(self) -> None:
        backend = _ScriptedBackend(
            [
                StreamChunk(delta="I am "),
                StreamChunk(delta="{{#happy}}"),
                StreamChunk(delta=" today"),
                _final(),
            ]
        )
        producer = VoiceModelReplyProducer(_context(backend))

        tokens = await _drain(producer)

        assert all("{{#" not in t for t in tokens)
        assert "".join(tokens) == "I am  today"

    @pytest.mark.asyncio
    async def test_tag_split_across_chunks_never_spoken(self) -> None:
        backend = _ScriptedBackend(
            [
                StreamChunk(delta="feeling {{#"),
                StreamChunk(delta="grate"),
                StreamChunk(delta="ful}} now"),
                _final(),
            ]
        )
        producer = VoiceModelReplyProducer(_context(backend))

        tokens = await _drain(producer)

        assert all("{{#" not in t for t in tokens)
        assert "".join(tokens) == "feeling  now"

    @pytest.mark.asyncio
    async def test_unclosed_tag_at_stream_end_not_spoken(self) -> None:
        backend = _ScriptedBackend([StreamChunk(delta="bye {{#hap"), _final()])
        producer = VoiceModelReplyProducer(_context(backend))

        tokens = await _drain(producer)

        assert all("{{#" not in t for t in tokens)
        assert "".join(tokens) == "bye "

    @pytest.mark.asyncio
    async def test_plain_speech_unaffected(self) -> None:
        backend = _ScriptedBackend([StreamChunk(delta="Hel"), StreamChunk(delta="lo!"), _final()])
        producer = VoiceModelReplyProducer(_context(backend))

        assert await _drain(producer) == ["Hel", "lo!"]


class TestR6CrisisEncoderWiring:
    """T8 — the R6 crisis encoder is wired into the voice path AND runs off the loop."""

    _EUPH = "i just want to fade out of the picture and not come back"
    _EXPLICIT = "i want to kill myself tonight"

    @pytest.mark.asyncio
    async def test_encoder_hard_bypasses_generation_and_runs_off_loop(self) -> None:
        import time

        from persona_runtime.crisis_encoder import CrisisEncoderError  # noqa: F401 — parity

        class _SlowSpy:
            def __init__(self) -> None:
                self.calls = 0

            def score(self, _text: str) -> float:
                self.calls += 1
                time.sleep(0.15)  # CPU-bound: proves the loop isn't blocked while it runs
                return 0.99

        from dataclasses import replace

        backend = _ScriptedBackend([StreamChunk(delta="MODEL_OUTPUT"), _final()])
        spy = _SlowSpy()
        ctx = replace(_context(backend), crisis_encoder=spy)  # VoiceTurnContext is frozen
        producer = VoiceModelReplyProducer(ctx)

        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            for _ in range(30):
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(_ticker())
        spoken = "".join(await _drain(producer, self._EUPH))
        await task

        assert spy.calls > 0, "the voice path never consulted the encoder"
        assert "MODEL_OUTPUT" not in spoken, "generation was NOT bypassed"
        assert "an AI" in spoken, "the spoken SafeCompletion was not emitted"
        assert ticks >= 8, f"the voice loop was starved while the score ran (ticks={ticks})"

    @pytest.mark.asyncio
    async def test_broken_encoder_keeps_lexical_hard(self) -> None:
        from persona_runtime.crisis_encoder import CrisisEncoderError

        class _Raises:
            def score(self, _text: str) -> float:
                msg = "boom"
                raise CrisisEncoderError(msg)

        from dataclasses import replace

        backend = _ScriptedBackend([StreamChunk(delta="MODEL_OUTPUT"), _final()])
        ctx = replace(_context(backend), crisis_encoder=_Raises())
        spoken = "".join(await _drain(VoiceModelReplyProducer(ctx), self._EXPLICIT))
        assert "MODEL_OUTPUT" not in spoken  # lexical-HARD fired despite the broken encoder
        assert "an AI" in spoken

    @pytest.mark.asyncio
    async def test_none_encoder_is_v11_no_bypass(self) -> None:
        backend = _ScriptedBackend([StreamChunk(delta="MODEL_OUTPUT"), _final()])
        producer = VoiceModelReplyProducer(_context(backend))  # ctx.crisis_encoder is None
        spoken = "".join(await _drain(producer, self._EUPH))
        assert "MODEL_OUTPUT" in spoken  # V11: euphemism generates normally


class TestExpressivityCapture:
    """V12 T3 — the producer captures the persona's stance tag (still stripped from
    the audio) and publishes the resolved expressivity to the listener (V12-D-2/D-5).

    This is the end-to-end assembly proof: N5's criterion-3 floor (tag never reaches
    TTS, even split across chunks) holds with the on_feeling capture fully wired.
    """

    @staticmethod
    def _producer_with_capture(
        backend: object, captured: list[VoiceExpressivity | None]
    ) -> VoiceModelReplyProducer:
        return VoiceModelReplyProducer(_context(backend), expressivity_listener=captured.append)

    @pytest.mark.asyncio
    async def test_lead_tag_published_and_stripped_from_audio(self) -> None:
        captured: list[VoiceExpressivity | None] = []
        backend = _ScriptedBackend(
            [StreamChunk(delta="{{#happy}}Hello"), StreamChunk(delta=" there."), _final()]
        )
        spoken = await _drain(self._producer_with_capture(backend, captured))
        assert "".join(spoken) == "Hello there."  # tag never in the spoken audio
        assert "{{#" not in "".join(spoken)
        assert captured == [EXPRESSIVITY_MAP["happy"]]  # resolved stance published

    @pytest.mark.asyncio
    async def test_no_tag_publishes_nothing(self) -> None:
        captured: list[VoiceExpressivity | None] = []
        backend = _ScriptedBackend([StreamChunk(delta="Just a flat line."), _final()])
        spoken = await _drain(self._producer_with_capture(backend, captured))
        assert "".join(spoken) == "Just a flat line."
        assert captured == []  # nothing published ⇒ backend resets to flat

    @pytest.mark.asyncio
    async def test_first_tag_wins_and_all_tags_stripped(self) -> None:
        captured: list[VoiceExpressivity | None] = []
        backend = _ScriptedBackend([StreamChunk(delta="{{#sad}}Oh. {{#happy}}Wait."), _final()])
        spoken = await _drain(self._producer_with_capture(backend, captured))
        assert "".join(spoken) == "Oh. Wait."  # BOTH tags stripped from audio
        assert captured == [EXPRESSIVITY_MAP["sad"]]  # only the FIRST tag published

    @pytest.mark.asyncio
    async def test_tag_split_across_chunks_still_captured_and_stripped(self) -> None:
        """N5's fuzz-invariant survives the full wiring: a split tag is captured once,
        never leaked to audio."""
        captured: list[VoiceExpressivity | None] = []
        backend = _ScriptedBackend(
            [
                StreamChunk(delta="{{#"),
                StreamChunk(delta="hap"),
                StreamChunk(delta="py}}Hi."),
                _final(),
            ]
        )
        spoken = await _drain(self._producer_with_capture(backend, captured))
        assert "".join(spoken) == "Hi."
        assert "{{#" not in "".join(spoken)
        assert captured == [EXPRESSIVITY_MAP["happy"]]

    @pytest.mark.asyncio
    async def test_listener_raising_never_breaks_stream_or_leaks_tag(self) -> None:
        """Fail-safe (V12-D-5): a raising listener degrades to flat — the spoken stream
        still completes and the tag is still stripped."""

        def _boom(_expr: VoiceExpressivity | None) -> None:
            raise RuntimeError("listener boom")

        backend = _ScriptedBackend(
            [StreamChunk(delta="{{#happy}}Hello"), StreamChunk(delta=" world."), _final()]
        )
        producer = VoiceModelReplyProducer(_context(backend), expressivity_listener=_boom)
        spoken = await _drain(producer)
        assert "".join(spoken) == "Hello world."  # stream uninterrupted
        assert "{{#" not in "".join(spoken)  # tag still stripped

    @pytest.mark.asyncio
    async def test_no_listener_is_byte_identical_pure_strip(self) -> None:
        """No expressivity listener ⇒ pure N5 STRIP, byte-identical (tag stripped, no publish)."""
        backend = _ScriptedBackend([StreamChunk(delta="{{#happy}}Hi."), _final()])
        producer = VoiceModelReplyProducer(_context(backend))  # no listener
        spoken = await _drain(producer)
        assert "".join(spoken) == "Hi."
        assert "{{#" not in "".join(spoken)
