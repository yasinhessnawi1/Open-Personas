"""The text loop names, prices and samples the model that served each call (R9-223/227/229).

Driven through REAL :class:`ConversationLoop` instances over ONE real
:class:`MultiModelChatBackend`, the way every conversation on a tier shares the tier's
cached chain in the api process. The assertions are on the TurnLog (the row the chat
deduct is charged from) and on the latency tracker the router reads.

* R9-223: a second conversation's call on the shared chain, made while the first turn
  is between its primary's failure and its fallback's answer, used to reset the ledger
  the first turn was read from, so the first turn was billed at the primary.
* R9-227: an image turn on a ``[non-vision, vision]`` tier was billed at the non-vision
  primary, which never saw the request.
* R9-229: a primary that answered on a retry was sampled as a clean first answer.
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG002
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal

import pytest
from _fakes import FakeStore  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.backends.errors import RateLimitError
from persona.backends.model_metadata import ModelMetadata
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import StreamChunk, TokenUsage
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.images import TurnImage
from persona_runtime.logging import MemoryTurnLogWriter, TurnLog
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.routing import FirstTokenLatencyTracker
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.asyncio

_CFG = BackendConfig(provider="anthropic", model="unused", api_key="sk-test")
_PROMPT, _COMPLETION = 1000, 500


def _rate(input_cents: float, output_cents: float) -> ModelMetadata:
    return ModelMetadata(
        cost_input_per_1k_tokens=input_cents,
        cost_output_per_1k_tokens=output_cents,
        latency_p50_ms=100.0,
        quality_benchmark=0.5,
        tools_supported=True,
        vision_supported=False,
        context_length=8192,
    )


#: 1000 prompt + 500 completion tokens: primary 2.0 cents, fallback 20.0 cents.
_RATES = {"vendor/primary": _rate(1.0, 2.0), "vendor/fallback": _rate(10.0, 20.0)}


class _RateTable:
    def resolve_with_source(
        self, model_id: str, *, allow_fetch: bool = True
    ) -> tuple[ModelMetadata, Literal["static", "catalog"]] | None:
        metadata = _RATES.get(model_id)
        return None if metadata is None else (metadata, "static")


def _transient() -> RateLimitError:
    return RateLimitError("429", context={"status_code": "429", "retry_after_s": "0.01"})


def _no_retry() -> RateLimitError:
    return RateLimitError("429", context={"status_code": "429", "reason": "insufficient_quota"})


class _Member:
    """A chain member: raises its scripted errors in turn, then answers every call."""

    supports_native_tools = False

    def __init__(
        self,
        model: str,
        *,
        errors: list[Exception] | None = None,
        vision: bool = False,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.provider_name = "vendor"
        self.model_name = model
        self.supports_vision = vision
        self.errors = list(errors or [])
        self.gate = gate
        self.stream_calls = 0

    async def chat_stream(self, messages: Any, **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.errors:
            raise self.errors.pop(0)
        yield StreamChunk(delta=f"hi from {self.model_name}")
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=_PROMPT,
                completion_tokens=_COMPLETION,
                total_tokens=_PROMPT + _COMPLETION,
            ),
        )


def _loop(
    chain: MultiModelChatBackend, tracker: FirstTokenLatencyTracker | None = None
) -> tuple[ConversationLoop, MemoryTurnLogWriter]:
    registry = TierRegistry(
        {t: TierConfig(name=t, backend_config=_CFG) for t in ("frontier", "mid", "small")}
    )
    registry._cache = {"frontier": chain, "mid": chain, "small": chain}  # type: ignore[assignment,attr-defined]  # noqa: SLF001
    writer = MemoryTurnLogWriter()
    loop = ConversationLoop(
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
        router=Router(),
        tier_registry=registry,
        turn_log_writer=writer,
        cost_source=_RateTable(),
        latency_tracker=tracker,
    )
    return loop, writer


async def _turn(
    loop: ConversationLoop, conversation_id: str, images: list[TurnImage] | None = None
) -> str:
    conversation = Conversation(conversation_id=conversation_id, persona_id="astrid", messages=[])
    return "".join([chunk.delta async for chunk in loop.turn(conversation, "hello", images=images)])


def _only(writer: MemoryTurnLogWriter) -> TurnLog:
    assert len(writer.logs) == 1
    return writer.logs[0]


async def test_a_concurrent_call_on_the_shared_chain_does_not_rename_the_turn() -> None:
    """R9-223: turn A's fallback answers after turn B ran start to end on the primary."""
    gate = asyncio.Event()
    primary = _Member("primary", errors=[_no_retry()])
    fallback = _Member("fallback", gate=gate)
    chain = MultiModelChatBackend([primary, fallback])
    loop_a, writer_a = _loop(chain)
    loop_b, writer_b = _loop(chain)

    turn_a = asyncio.create_task(_turn(loop_a, "a"))
    while primary.errors:  # A's primary has failed; A now waits on the fallback
        await asyncio.sleep(0)
    await _turn(loop_b, "b")  # B: the primary answers
    gate.set()
    reply_a = await turn_a

    assert "hi from fallback" in reply_a
    log_a, log_b = _only(writer_a), _only(writer_b)
    assert (log_a.model_name, log_a.cost_cents) == ("fallback", 20.0)
    assert (log_b.model_name, log_b.cost_cents) == ("primary", 2.0)
    assert loop_a.last_turn_cost_cents == 20.0
    assert (log_a.tier_fallback_count, log_a.tier_fallback_reasons) == (1, ["RateLimitError"])
    assert (log_b.tier_fallback_count, log_b.fallback_engaged) == (0, False)


async def test_an_image_turn_is_billed_at_the_vision_model_that_saw_it() -> None:
    """R9-227: the walk skips the non-vision primary; the bill must not name it."""
    primary = _Member("primary")
    vision = _Member("fallback", vision=True)
    loop, writer = _loop(MultiModelChatBackend([primary, vision]))

    image = TurnImage(workspace_path="uploads/cat.png", media_type="image/png", content_bytes=b"x")
    await _turn(loop, "c", images=[image])

    assert primary.stream_calls == 0
    log = _only(writer)
    assert (log.model_name, log.cost_cents) == ("fallback", 20.0)
    assert (log.tier_fallback_count, log.fallback_engaged) == (0, False)


async def test_a_primary_that_answered_on_a_retry_gives_no_latency_sample() -> None:
    """R9-229 (D-R9-226 option ii): the time includes the failed try and the retry wait."""
    tracker = FirstTokenLatencyTracker()
    primary = _Member("primary", errors=[_transient()])
    loop, writer = _loop(MultiModelChatBackend([primary, _Member("fallback")]), tracker)

    await _turn(loop, "d")

    assert primary.stream_calls == 2
    assert (tracker.sample_count("primary"), tracker.sample_count("fallback")) == (0, 0)
    # A retry on the backend that answered is not a fallback.
    log = _only(writer)
    assert (log.model_name, log.tier_fallback_count, log.fallback_engaged) == ("primary", 0, False)


async def test_a_primary_that_answered_first_time_still_gives_one_sample() -> None:
    tracker = FirstTokenLatencyTracker()
    loop, _ = _loop(MultiModelChatBackend([_Member("primary"), _Member("fallback")]), tracker)

    await _turn(loop, "e")

    assert (tracker.sample_count("primary"), tracker.sample_count("fallback")) == (1, 0)
