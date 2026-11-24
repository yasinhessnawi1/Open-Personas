"""T8 — the voice path actually uses the overlapped graph (K3-D-6 wiring).

Proves the voice half of the wiring end-to-end: the assembler injects a taken
GraphContext into the shared PromptBuilder (so a voice prompt renders graph
knowledge exactly as chat does), and the reply producer starts the owner-scoped
query at turn onset + takes it before assembly. The zero-serial overlap PRIMITIVE
itself is proven in test_graph_voice.py; here we prove the voice composition uses
it — the graph node a persona learned through another persona reaches the spoken
turn's prompt.
"""

# ruff: noqa: ANN401, ARG001, ARG002 — test doubles with intentionally loose signatures.

from __future__ import annotations

import asyncio
import dataclasses
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity, RoutingConfig
from persona_runtime.graph_voice import start_graph_retrieval
from persona_runtime.prompt import (
    GraphContext,
    GraphKnowledgeItem,
    GraphRecency,
    PromptBuilder,
    RetrievedContext,
)
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.loop.streaming import Transcript
from persona_voice.model import VoiceModelReplyProducer, VoiceTurnContext
from persona_voice.model import reply_producer as reply_producer_module
from persona_voice.model.prompt_assembler import VoicePromptAssembler


def _chunk(text: str) -> PersonaChunk:
    return PersonaChunk(
        id=f"id-{abs(hash(text)) % 10000}", text=text, metadata={}, created_at=datetime.now(UTC)
    )


def _known_graph() -> GraphContext:
    return GraphContext(
        items=(
            GraphKnowledgeItem(
                concept_name="focus struggles",
                content="Finds long study sessions hard to concentrate through.",
                recency=GraphRecency.A_WHILE_BACK,
                source_persona="kai",
            ),
        )
    )


#: Bounds a FAILURE only: a passing run waits just until the graph query settles.
_GATE_TIMEOUT_S = 10.0


class _FakeStore:
    def __init__(
        self,
        *,
        all_chunks: list[PersonaChunk] | None = None,
        read_gate: threading.Event | None = None,
    ) -> None:
        self._all = all_chunks or []
        self._read_gate = read_gate

    def write(self, persona_id: str, chunks: list[PersonaChunk], **kwargs: Any) -> None: ...

    def query(self, persona_id: str, query: str, top_k: int, **filters: Any) -> list[PersonaChunk]:
        return []

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        # R9-319: when gated, this off-loop persona read finishes only after the
        # overlapped graph query has settled on the loop. That is the "ready by
        # assembly" case on purpose, signalled by the query's own future, instead
        # of a sleep that a loaded CI runner could outlast.
        if self._read_gate is not None:
            assert self._read_gate.wait(_GATE_TIMEOUT_S), "the overlapped graph query never settled"
        return list(self._all)

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return []

    def delete(self, persona_id: str) -> None: ...


class _ScriptedBackend:
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
        identity=PersonaIdentity(name="Astrid", role="assistant", background="."),
        routing=RoutingConfig(tier_for_generation="frontier"),
    )


def _stores(read_gate: threading.Event | None = None) -> dict[str, _FakeStore]:
    return {
        "identity": _FakeStore(all_chunks=[_chunk("I am Astrid.")], read_gate=read_gate),
        "self_facts": _FakeStore(),
        "worldview": _FakeStore(),
        "episodic": _FakeStore(),
    }


def _context(
    backend: object,
    *,
    graph_retrieval: object | None = None,
    read_gate: threading.Event | None = None,
) -> VoiceTurnContext:
    from persona.backends import BackendConfig

    cfg = BackendConfig(provider="anthropic", model="test-model", api_key=None)  # type: ignore[arg-type]
    registry = TierRegistry({"frontier": TierConfig(name="frontier", backend_config=cfg)})
    registry._cache = {"frontier": backend}  # type: ignore[assignment,dict-item]  # noqa: SLF001
    return VoiceTurnContext(
        persona=_persona(),
        stores=_stores(read_gate),  # type: ignore[arg-type]
        conversation=Conversation(conversation_id="c1", persona_id="astrid", messages=[]),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        graph_retrieval=graph_retrieval,  # type: ignore[arg-type]
    )


@pytest.fixture
def graph_settled(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """An event set once the producer's overlapped graph query has settled.

    Wraps the REAL :func:`start_graph_retrieval` the producer calls, so the real
    overlap and the real ``take_graph_if_ready`` still run; it only observes the
    returned future. The done callback runs on the loop after the future is done,
    so a store read gated on this event cannot finish before the graph is ready.
    """
    settled = threading.Event()

    def observed(
        graph_retrieval: Callable[[str], GraphContext], query: str
    ) -> asyncio.Future[GraphContext]:
        future = start_graph_retrieval(graph_retrieval, query)
        future.add_done_callback(lambda _f: settled.set())
        return future

    monkeypatch.setattr(reply_producer_module, "start_graph_retrieval", observed)
    return settled


class TestAssemblerInjectsGraph:
    """The deterministic seam: a taken GraphContext reaches the voice prompt."""

    def test_build_injects_the_graph_into_the_voice_prompt(self) -> None:
        ctx = _context(_ScriptedBackend([]))
        assembler = VoicePromptAssembler(ctx)
        messages = assembler.build(
            "help me study", history=[], max_tokens=8000, graph=_known_graph()
        )
        system = messages[0].content
        assert "What you already know about this person:" in system
        assert "Finds long study sessions hard to concentrate through." in system

    def test_build_without_graph_is_graph_off(self) -> None:
        ctx = _context(_ScriptedBackend([]))
        assembler = VoicePromptAssembler(ctx)
        messages = assembler.build("hi", history=[], max_tokens=8000)
        assert "What you already know about this person:" not in messages[0].content

    def test_retrieved_context_is_frozen_so_model_copy_injection_is_safe(self) -> None:
        # The injection path uses RetrievedContext.model_copy — confirm it's frozen
        # (the pattern the assembler relies on).
        assert RetrievedContext.model_config["frozen"] is True


def _final() -> StreamChunk:
    return StreamChunk(
        delta="",
        is_final=True,
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


class TestProducerUsesOverlappedGraph:
    @pytest.mark.asyncio
    async def test_producer_uses_the_overlapped_graph_when_ready(
        self, graph_settled: threading.Event
    ) -> None:
        # The graph query has settled by assembly time (the persona read waits on
        # its future), so the node lands in the prompt the voice model receives.
        seen_query: list[str] = []

        def fast_graph(query: str) -> GraphContext:
            seen_query.append(query)
            return _known_graph()

        backend = _ScriptedBackend([StreamChunk(delta="ok"), _final()])
        producer = VoiceModelReplyProducer(
            _context(backend, graph_retrieval=fast_graph, read_gate=graph_settled)
        )

        stream = await producer(Transcript(is_final=True, text="help me study", confidence=1.0))
        _ = [tok async for tok in stream]

        assert seen_query == ["help me study"]  # the owner-scoped query ran, scoped to the turn
        assert backend.captured_prompt is not None
        system = backend.captured_prompt[0].content
        assert "Finds long study sessions hard to concentrate through." in system

    @pytest.mark.asyncio
    async def test_graph_off_when_not_wired_is_byte_clean(self) -> None:
        backend = _ScriptedBackend([StreamChunk(delta="ok"), _final()])
        producer = VoiceModelReplyProducer(_context(backend, graph_retrieval=None))
        stream = await producer(Transcript(is_final=True, text="hi", confidence=1.0))
        _ = [tok async for tok in stream]
        assert backend.captured_prompt is not None
        assert "What you already know about this person:" not in backend.captured_prompt[0].content

    def test_voice_turn_context_carries_graph_retrieval(self) -> None:
        ctx = _context(_ScriptedBackend([]), graph_retrieval=lambda _q: GraphContext())
        # dataclasses.replace works (frozen dc) — the field is a real composable.
        replaced = dataclasses.replace(ctx, graph_retrieval=None)
        assert replaced.graph_retrieval is None


class TestK4WindowReachesTheGate:
    """The K4 gate sees a NON-EMPTY recent window on a real VOICE turn (Spec K4, T6).

    Voice especially: an uncanny topic-evasion mid-call is the worst failure, and voice
    threads context differently (the graph query runs off-thread). The window is published
    by the reply producer in the turn coroutine BEFORE the off-thread query, and must
    propagate into the gate through the context copy start_graph_retrieval makes at
    kick-off. Proven by driving a real producer turn and asserting the gate observed the
    prior conversation, NOT by priming the ContextVar (which would prove the gate reads it,
    not that the producer sets it).
    """

    @pytest.mark.asyncio
    async def test_real_voice_turn_publishes_the_window_to_the_gate(
        self, graph_settled: threading.Event
    ) -> None:
        from persona.graph.config import GraphSettings
        from persona.graph.fusion import HybridResult
        from persona_runtime.graph_selection import GatingContext, make_graph_retrieval
        from persona_runtime.graph_window import get_recent_window

        seen: list[tuple[str, ...]] = []

        def allowlist_provider(gctx: GatingContext) -> set[str] | None:
            seen.append(gctx.recent_messages)
            return None

        class _EmptyRetriever:
            def retrieve(
                self, owner_id: str, query: str, *, allowlist: Any = None, top_k: Any = None
            ) -> list[HybridResult]:
                return []

        # The REAL composition: recent_window_provider is the actual ContextVar reader, so
        # the gate sees a window only if the producer set it AND it crossed the thread.
        graph_retrieval = make_graph_retrieval(
            retriever=_EmptyRetriever(),
            owner_provider=lambda: "astrid",
            settings=GraphSettings(),
            allowlist_provider=allowlist_provider,
            recent_window_provider=get_recent_window,
        )
        backend = _ScriptedBackend([StreamChunk(delta="ok"), _final()])
        # The gate runs inside the off-thread query; gating the persona read on its
        # future means it has run by the time the turn ends (R9-319).
        ctx = _context(backend, graph_retrieval=graph_retrieval, read_gate=graph_settled)
        # A prior turn opened the topic; the new transcript is a bland follow-up that does
        # NOT re-name it (the uncanny-re-closing scenario, mid-call).
        ctx = dataclasses.replace(
            ctx,
            conversation=Conversation(
                conversation_id="c1",
                persona_id="astrid",
                messages=[
                    ConversationMessage(
                        role="user",
                        content="i keep having panic attacks lately",
                        created_at=datetime.now(UTC),
                    ),
                ],
            ),
        )
        producer = VoiceModelReplyProducer(ctx)
        stream = await producer(
            Transcript(is_final=True, text="and what should i do next", confidence=1.0)
        )
        _ = [tok async for tok in stream]

        assert seen, "the voice gate never ran (graph retrieval did not execute on the turn)"
        assert seen[0], (
            "the window was EMPTY in voice — the producer did not publish it cross-thread"
        )
        assert any("panic attacks" in m for m in seen[0])
