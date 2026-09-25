"""Voice records first-token latency only for a model that answered first time (R9-226).

The producer used to record the turn's first-token latency against
``backend.model_name``, which on a multi-model chain is the PRIMARY by the wrapper's
contract. So a turn a fallback answered charged its latency, plus the primary's own
failure time, to the primary, and credited the fallback with nothing.

Driven through a REAL :class:`VoiceModelReplyProducer` over a REAL
:class:`MultiModelChatBackend` whose primary genuinely raises a 429 inside
``chat_stream``, with a real :class:`FirstTokenLatencyTracker` in the turn context.
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG002
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends import BackendConfig, StreamChunk, TokenUsage
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity, RoutingConfig
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.routing import FirstTokenLatencyTracker
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.loop.streaming import Transcript
from persona_voice.model import VoiceModelReplyProducer, VoiceTurnContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.asyncio


class _Speaker:
    """A backend that answers every call with one sentence naming itself, in two deltas."""

    supports_native_tools = False
    supports_vision = False

    def __init__(self, provider: str, model: str) -> None:
        self.provider_name = provider
        self.model_name = model
        self.stream_calls = 0

    async def chat_stream(self, messages: list[Any], **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        yield StreamChunk(delta="Hi from ")
        yield StreamChunk(delta=f"{self.model_name}. ")
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class _RateLimitedPrimary(_Speaker):
    """A primary whose stream raises a 429 before its first chunk, as a real one does."""

    async def chat_stream(self, messages: list[Any], **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        raise RateLimitError("scripted 429", context={"provider": self.provider_name})
        yield StreamChunk(delta="")  # pragma: no cover - makes this an async generator


class _Store:
    def query(self, persona_id: str, query: str, top_k: int, **filters: Any) -> list[PersonaChunk]:
        return []

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return [
            PersonaChunk(id="id-1", text="I am Astrid.", metadata={}, created_at=datetime.now(UTC))
        ]

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return []


def _producer(
    chain: MultiModelChatBackend, tracker: FirstTokenLatencyTracker
) -> VoiceModelReplyProducer:
    cfg = BackendConfig(provider="anthropic", model="unused", api_key=None)  # type: ignore[arg-type]
    registry = TierRegistry(
        {"frontier": TierConfig(name="frontier", backend_config=cfg, preconstructed_backend=chain)}
    )
    kinds = ("identity", "self_facts", "worldview", "episodic")
    return VoiceModelReplyProducer(
        VoiceTurnContext(
            persona=Persona(
                persona_id="astrid",
                identity=PersonaIdentity(name="Astrid", role="assistant", background="b"),
                routing=RoutingConfig(tier_for_generation="frontier"),
            ),
            stores={kind: _Store() for kind in kinds},  # type: ignore[misc]
            conversation=Conversation(conversation_id="c1", persona_id="astrid", messages=[]),
            prompt_builder=PromptBuilder(),
            router=Router(),
            tier_registry=registry,
            history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
            latency_tracker=tracker,
        )
    )


async def _spoken(producer: VoiceModelReplyProducer) -> str:
    stream = await producer(Transcript(is_final=True, text="what are my rights?", confidence=1.0))
    return "".join([token async for token in stream])


async def test_a_turn_a_fallback_answered_records_no_latency_for_either_model() -> None:
    primary = _RateLimitedPrimary("nvidia", "primary-model")
    fallback = _Speaker("anthropic", "fallback-model")
    chain = MultiModelChatBackend(
        [primary, fallback],  # type: ignore[list-item]
        tier_name="frontier",
        max_retries_per_backend=0,
    )
    tracker = FirstTokenLatencyTracker()

    spoken = await _spoken(_producer(chain, tracker))

    # The chain genuinely fell back, and still names the primary by contract.
    assert spoken == "Hi from fallback-model. "
    assert primary.stream_calls == 1
    assert chain.model_name == "primary-model"
    assert (tracker.sample_count("primary-model"), tracker.sample_count("fallback-model")) == (
        0,
        0,
    )


async def test_a_turn_the_primary_answered_records_one_sample_for_the_primary() -> None:
    # Two deltas, one sample: the first-token latency is recorded once per turn.
    primary = _Speaker("nvidia", "primary-model")
    fallback = _Speaker("anthropic", "fallback-model")
    chain = MultiModelChatBackend(
        [primary, fallback],  # type: ignore[list-item]
        tier_name="frontier",
        max_retries_per_backend=0,
    )
    tracker = FirstTokenLatencyTracker()

    spoken = await _spoken(_producer(chain, tracker))

    assert spoken == "Hi from primary-model. "
    assert fallback.stream_calls == 0
    assert (tracker.sample_count("primary-model"), tracker.sample_count("fallback-model")) == (
        1,
        0,
    )
