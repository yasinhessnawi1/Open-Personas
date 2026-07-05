"""Real-seam tests for P9 voice routing (T4; criterion 3).

Voice generation resolves the NAMED LATENCY TIER (mid) through the real
:class:`VoiceModelReplyProducer` path — the production composition
(:class:`PolicyRouter`, exactly what ``agent/runner.py`` wires) — proven by
which tier's backend actually generated the reply, not by a unit of
``tier_for()``. Locks: no turn-1 frontier (the pre-P9 bare-``Router()``
hazard — a first voice turn on GLM 5.2's measured 6.5 s TTFT blows the
800 ms budget), no boilerplate dip to small (measured SLOWER than mid,
P9-R-2), pin honored, and the context carries the voice profile.
"""

# ruff: noqa: ANN401, ARG002 — test doubles with intentionally loose signatures.

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends import BackendConfig, StreamChunk, TokenUsage
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity, RoutingConfig
from persona_runtime.prompt import PromptBuilder
from persona_runtime.routing import PolicyRouter
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.loop.streaming import Transcript
from persona_voice.model import VoiceModelReplyProducer, VoiceTurnContext


class _FakeStore:
    def query(self, persona_id: str, query: str, top_k: int, **filters: Any) -> list[PersonaChunk]:
        return []

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return [
            PersonaChunk(
                id="id-1",
                text="I am Astrid.",
                metadata={},
                created_at=datetime.now(UTC),
            )
        ]

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return []

    def write(self, persona_id: str, chunks: list[PersonaChunk], **kwargs: Any) -> None:
        return None

    def delete(self, persona_id: str) -> None:
        return None


class _TierBackend:
    """Streaming double that records whether IT served the generation."""

    provider_name = "anthropic"
    supports_native_tools = False
    supports_vision = False

    def __init__(self, tier: str) -> None:
        self.model_name = f"model-{tier}"
        self.tier = tier
        self.served = 0

    async def chat_stream(
        self,
        messages: list[Any],
        *,
        tools: Any = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: Any = None,
    ) -> AsyncIterator[StreamChunk]:
        self.served += 1
        yield StreamChunk(delta="Hei!")
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
        )


def _persona(*, pin: str = "auto") -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="tenancy assistant",
            background="Knows husleieloven.",
            constraints=["Never give binding advice."],
        ),
        routing=RoutingConfig(tier_for_generation=pin),  # type: ignore[arg-type]
    )


def _conversation(turns: int) -> Conversation:
    msgs = [
        ConversationMessage(
            role="user" if i % 2 == 0 else "assistant",
            content=f"turn {i}",
            created_at=datetime.now(UTC),
        )
        for i in range(turns)
    ]
    return Conversation(conversation_id="c1", persona_id="astrid", messages=msgs)


def _producer(
    *, turns: int = 0, pin: str = "auto"
) -> tuple[VoiceModelReplyProducer, dict[str, _TierBackend]]:
    cfg = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
    backends = {t: _TierBackend(t) for t in ("frontier", "mid", "small")}
    registry = TierRegistry(
        {t: TierConfig(name=t, backend_config=cfg) for t in ("frontier", "mid", "small")}
    )
    registry._cache = dict(backends)  # type: ignore[assignment]  # noqa: SLF001
    stores = {k: _FakeStore() for k in ("identity", "self_facts", "worldview", "episodic")}
    ctx = VoiceTurnContext(
        persona=_persona(pin=pin),
        stores=stores,  # type: ignore[arg-type]
        conversation=_conversation(turns),
        prompt_builder=PromptBuilder(),
        # The PRODUCTION composition (agent/runner.py wires exactly this).
        router=PolicyRouter(tier_registry=registry),
        tier_registry=registry,
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
    )
    return VoiceModelReplyProducer(ctx), backends


async def _speak(producer: VoiceModelReplyProducer, text: str) -> None:
    stream = await producer(Transcript(is_final=True, text=text, confidence=1.0))
    async for _ in stream:
        pass


class TestVoiceResolvesTheLatencyTier:
    @pytest.mark.asyncio
    async def test_turn_1_generates_on_mid_not_frontier(self) -> None:
        # The pre-P9 hazard killed: bare Router()'s first-turn rule put voice
        # turn 1 on frontier (measured 6.5s median TTFT vs the 800ms budget).
        producer, backends = _producer(turns=0)
        await _speak(producer, "hei, hvem er du?")
        assert backends["mid"].served == 1
        assert backends["frontier"].served == 0

    @pytest.mark.asyncio
    async def test_turn_n_generates_on_mid(self) -> None:
        producer, backends = _producer(turns=6)
        await _speak(producer, "fortell mer om depositum")
        assert backends["mid"].served == 1
        assert backends["frontier"].served == 0

    @pytest.mark.asyncio
    async def test_boilerplate_does_not_dip_to_small(self) -> None:
        # small's primary is measured SLOWER than mid (P9-R-2: 926ms vs 57ms
        # median TTFT) — the old boilerplate→small rule was a latency loss too.
        producer, backends = _producer(turns=4)
        await _speak(producer, "ok thanks")
        assert backends["mid"].served == 1
        assert backends["small"].served == 0

    @pytest.mark.asyncio
    async def test_context_carries_the_voice_profile(self) -> None:
        producer, _ = _producer()
        assert producer._routing_context("hei").profile == "voice"  # noqa: SLF001


class TestPinStillHonored:
    @pytest.mark.asyncio
    async def test_frontier_pin_generates_on_frontier(self) -> None:
        # A deliberate override (P9-D-7) — the operator/user chose it; the
        # latency tradeoff is theirs to make.
        producer, backends = _producer(turns=0, pin="frontier")
        await _speak(producer, "hei")
        assert backends["frontier"].served == 1
        assert backends["mid"].served == 0
