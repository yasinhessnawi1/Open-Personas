"""A chat turn is priced round by round, each at the model that served it (R9-246).

Driven through a REAL :class:`ConversationLoop` over a REAL
:class:`MultiModelChatBackend`: a tool turn whose first round the primary serves and
whose follow-up a fallback serves (the primary failed on the second call). The
assertion is on the TurnLog row and on ``last_turn_cost_cents``, the figure the chat
worker charges from. The same turn shape in voice is priced 22.0 cents, not 40.0
(``voice/tests/unit/billing/test_fallback_turn_pricing.py``); chat now agrees.
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG002
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import pytest
from _fakes import FakeStore  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.backends.errors import RateLimitError
from persona.backends.model_metadata import ModelMetadata
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import StreamChunk, TokenUsage, ToolCallDelta
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolResult
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.logging import MemoryTurnLogWriter, TurnLog
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
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


#: One round (1000 in, 500 out): cheap 2.0 cents, dear 20.0 cents.
_RATES = {"vendor/cheap": _rate(1.0, 2.0), "vendor/dear": _rate(10.0, 20.0)}


class _RateTable:
    def resolve_with_source(
        self, model_id: str, *, allow_fetch: bool = True
    ) -> tuple[ModelMetadata, Literal["static", "catalog"]] | None:
        metadata = _RATES.get(model_id)
        return None if metadata is None else (metadata, "static")


@tool(name="echo", description="Echo a message back.")
async def _echo_tool(message: str) -> ToolResult:
    return ToolResult(tool_name="echo", content=f"echoed: {message}", is_error=False)


def _no_retry() -> RateLimitError:
    return RateLimitError("429", context={"status_code": "429", "reason": "insufficient_quota"})


class _Member:
    """Plays one scripted outcome per call: ``"tool"``, ``"text"`` or an exception."""

    supports_native_tools = False
    supports_vision = False

    def __init__(
        self,
        model: str,
        script: list[object],
        *,
        provider: str = "vendor",
        cost_usd: float | None = None,
    ) -> None:
        self.provider_name = provider
        self.model_name = model
        self.script = list(script)
        self.cost_usd = cost_usd

    async def chat_stream(self, messages: Any, **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "tool":
            yield StreamChunk(
                delta="",
                tool_call_delta=ToolCallDelta(
                    call_id=f"call-{len(self.script)}",
                    name_delta="echo",
                    arguments_delta='{"message": "x"}',
                ),
            )
        else:
            yield StreamChunk(delta=f"answer from {self.model_name}")
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=_PROMPT,
                completion_tokens=_COMPLETION,
                total_tokens=_PROMPT + _COMPLETION,
                cost_usd=self.cost_usd,
            ),
        )


def _loop(
    chain: MultiModelChatBackend, *, max_tool_rounds: int = 5
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
        toolbox=Toolbox([_echo_tool], allow_list=None),  # type: ignore[list-item]
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        turn_log_writer=writer,
        cost_source=_RateTable(),
        max_tool_rounds=max_tool_rounds,
    )
    return loop, writer


async def _turn(loop: ConversationLoop) -> TurnLog:
    conversation = Conversation(conversation_id="c1", persona_id="astrid", messages=[])
    _ = [chunk async for chunk in loop.turn(conversation, "please echo x")]
    writer = loop._turn_log_writer  # noqa: SLF001
    assert isinstance(writer, MemoryTurnLogWriter)
    assert len(writer.logs) == 1
    return writer.logs[0]


async def test_a_cheap_tool_round_and_a_dear_fallback_follow_up_cost_22_not_40() -> None:
    cheap = _Member("cheap", ["tool", _no_retry()])
    dear = _Member("dear", ["text"])
    loop, _ = _loop(MultiModelChatBackend([cheap, dear]))

    log = await _turn(loop)

    # Pre-fix: 2000 in + 1000 out at the last round's model (dear) = 40.0.
    assert log.cost_cents == pytest.approx(22.0)
    assert loop.last_turn_cost_cents == pytest.approx(22.0)
    assert (log.cost_basis, loop.last_turn_cost_basis) == ("estimate_static", "estimate_static")
    # Attribution stays the last round's served model (D5); tokens stay summed (I2).
    assert (log.model_name, log.prompt_tokens, log.completion_tokens) == ("dear", 2000, 1000)


async def test_a_dear_tool_round_and_a_cheap_fallback_follow_up_cost_22_not_4() -> None:
    dear = _Member("dear", ["tool", _no_retry()])
    cheap = _Member("cheap", ["text"])
    loop, _ = _loop(MultiModelChatBackend([dear, cheap]))

    log = await _turn(loop)

    # Pre-fix: 2000 in + 1000 out at cheap = 4.0.
    assert log.cost_cents == pytest.approx(22.0)
    assert log.model_name == "cheap"


async def test_a_rounds_openrouter_actual_survives_a_turn_whose_other_round_has_none() -> None:
    routed = _Member("z-ai/glm-4.6", ["tool", _no_retry()], provider="openrouter", cost_usd=0.05)
    dear = _Member("dear", ["text"])
    loop, _ = _loop(MultiModelChatBackend([routed, dear]))

    log = await _turn(loop)

    # 5.0 cents actual for the routed round + 20.0 estimated for the fallback round.
    assert log.cost_cents == pytest.approx(25.0)
    assert log.cost_basis == "estimate_static"


async def test_every_round_is_priced_once_including_the_cap_forced_final_round() -> None:
    """The priced rounds are exactly the rounds the TurnLog's token sums count."""
    cheap = _Member("cheap", ["tool", "tool", "text"])
    loop, _ = _loop(MultiModelChatBackend([cheap]), max_tool_rounds=1)

    log = await _turn(loop)

    assert (log.prompt_tokens, log.completion_tokens) == (3000, 1500)
    assert log.cost_cents == pytest.approx(6.0)
