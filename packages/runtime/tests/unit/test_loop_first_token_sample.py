"""The text loop records first-token latency only for a model that answered first time (R9-226).

The loop used to record every round's first-token latency against
``backend.model_name``, which on a multi-model chain is the PRIMARY by the wrapper's
contract. So a round a fallback answered charged its latency, plus the primary's own
failure time, to the primary, and credited the fallback with nothing.

Driven through a REAL :class:`ConversationLoop` over a REAL
:class:`MultiModelChatBackend` whose primary genuinely raises a 429 inside
``chat_stream``, with a real :class:`FirstTokenLatencyTracker` wired in. The
assertion is on the tracker, the thing the router reads.
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG002
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.routing import FirstTokenLatencyTracker, HeuristicRouter
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends.types import StreamChunk
    from persona.schema.conversation import ConversationMessage

pytestmark = pytest.mark.asyncio

_CFG = BackendConfig(provider="anthropic", model="unused", api_key="sk-test")


class _RateLimitedPrimary:
    """A primary whose stream raises a 429 before its first chunk, as a real one does."""

    provider_name = "nvidia"
    model_name = "primary-model"
    supports_native_tools = False
    supports_vision = False

    def __init__(self) -> None:
        self.stream_calls = 0

    async def chat_stream(
        self, messages: list[ConversationMessage], **_kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        raise RateLimitError("scripted 429", context={"provider": self.provider_name})
        yield  # pragma: no cover - makes this an async generator


def _loop(chain: MultiModelChatBackend, tracker: FirstTokenLatencyTracker) -> ConversationLoop:
    registry = TierRegistry(
        {"frontier": TierConfig(name="frontier", backend_config=_CFG, preconstructed_backend=chain)}
    )
    return ConversationLoop(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(name="Astrid", role="assistant", background="bg"),
        ),
        stores={k: FakeStore() for k in ("identity", "self_facts", "worldview", "episodic")},  # type: ignore[arg-type, misc]
        toolbox=Toolbox([], allow_list=None),
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=HeuristicRouter(tier_registry=registry),
        tier_registry=registry,
        turn_log_writer=MemoryTurnLogWriter(),
        latency_tracker=tracker,
    )


async def _reply(loop: ConversationLoop) -> str:
    conversation = Conversation(conversation_id="c1", persona_id="astrid", messages=[])
    return "".join([chunk.delta async for chunk in loop.turn(conversation, "hello")])


async def test_a_round_a_fallback_answered_records_no_latency_for_either_model() -> None:
    primary = _RateLimitedPrimary()
    fallback = ScriptedBackend(
        [ScriptedRound(text_deltas=["fallback ", "says ", "hi"])],
        provider_name="anthropic",
        model_name="fallback-model",
    )
    chain = MultiModelChatBackend([primary, fallback], max_retries_per_backend=0)  # type: ignore[list-item]
    tracker = FirstTokenLatencyTracker()

    reply = await _reply(_loop(chain, tracker))

    # The chain genuinely fell back, and still names the primary by contract.
    assert "fallback says hi" in reply
    assert primary.stream_calls == 1
    assert chain.model_name == "primary-model"
    assert (tracker.sample_count("primary-model"), tracker.sample_count("fallback-model")) == (
        0,
        0,
    )


async def test_a_round_the_primary_answered_records_one_sample_for_the_primary() -> None:
    # Several deltas, one sample: the first-token latency is recorded once per round.
    primary = ScriptedBackend(
        [ScriptedRound(text_deltas=["primary ", "says ", "hi"])],
        provider_name="nvidia",
        model_name="primary-model",
    )
    fallback = ScriptedBackend([], provider_name="anthropic", model_name="fallback-model")
    chain = MultiModelChatBackend([primary, fallback], max_retries_per_backend=0)
    tracker = FirstTokenLatencyTracker()

    reply = await _reply(_loop(chain, tracker))

    assert "primary says hi" in reply
    assert fallback.chat_stream_calls == 0
    assert (tracker.sample_count("primary-model"), tracker.sample_count("fallback-model")) == (
        1,
        0,
    )
