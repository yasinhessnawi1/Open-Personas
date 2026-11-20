"""A chat turn that compacts its history is charged for the compaction (R9-299, D-M3-1).

Once a conversation passes ``compact_every`` messages, the turn first folds the older
messages into the running summary with a model call on the background tier
(``ConversationLoop._summarise``). Before R9-299 that call's usage went nowhere: the
turn was priced from its answer rounds alone, so the summary was a provider call the
owner was never charged for. The chat worker charges ``last_turn_cost_cents``, so that
figure (and the TurnLog row it is recorded beside) is what these tests pin.

Driven through a REAL :class:`ConversationLoop`: the answer is served by one model and
the summary by another, at rates that make each part's share visible in the total.
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG002
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from _fakes import FakeStore  # type: ignore[import-not-found]
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import ChatResponse, StreamChunk, TokenUsage
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter, TurnLog
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from test_loop_per_round_pricing import (  # type: ignore[import-not-found]
    _CFG,
    _COMPLETION,
    _PROMPT,
    _echo_tool,
    _Member,
    _RateTable,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.asyncio

#: The summary's own usage: 1000 in, 500 out, which is 2.0 cents at ``vendor/cheap``.
_SUMMARY_PROMPT, _SUMMARY_COMPLETION = 1000, 500


class _Summariser:
    """The background tier: a non-streaming ``chat`` that reports its own usage."""

    provider_name = "vendor"
    model_name = "cheap"
    supports_native_tools = False
    supports_vision = False

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Any, **_kwargs: Any) -> ChatResponse:
        self.calls += 1
        return ChatResponse(
            content="they talked about the deposit",
            tool_calls=[],
            usage=TokenUsage(
                prompt_tokens=_SUMMARY_PROMPT,
                completion_tokens=_SUMMARY_COMPLETION,
                total_tokens=_SUMMARY_PROMPT + _SUMMARY_COMPLETION,
            ),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=1.0,
        )

    async def chat_stream(self, messages: Any, **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        raise AssertionError("the summary is a non-streaming call")
        yield  # pragma: no cover


def _loop(answer: _Member, summariser: _Summariser, **extra: Any) -> ConversationLoop:
    chain = MultiModelChatBackend([answer])
    registry = TierRegistry(
        {t: TierConfig(name=t, backend_config=_CFG) for t in ("frontier", "mid", "small")}
    )
    # The answer on the chat tiers; the summary on the background (small) tier.
    registry._cache = {"frontier": chain, "mid": chain, "small": summariser}  # type: ignore[assignment,attr-defined]  # noqa: SLF001
    return ConversationLoop(
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
        turn_log_writer=MemoryTurnLogWriter(),
        cost_source=_RateTable(),
        **extra,
    )


def _conversation(prior_messages: int) -> Conversation:
    now = datetime.now(UTC)
    return Conversation(
        conversation_id="c1",
        persona_id="astrid",
        messages=[
            ConversationMessage(
                role="user" if i % 2 == 0 else "assistant", content=f"turn {i}", created_at=now
            )
            for i in range(prior_messages)
        ],
    )


async def _turn(loop: ConversationLoop, conversation: Conversation) -> tuple[TurnLog, StreamChunk]:
    chunks = [chunk async for chunk in loop.turn(conversation, "and the deposit?")]
    writer = loop._turn_log_writer  # noqa: SLF001
    assert isinstance(writer, MemoryTurnLogWriter)
    assert len(writer.logs) == 1
    return writer.logs[0], chunks[-1]


async def test_a_compacting_turn_is_charged_for_its_summary() -> None:
    summariser = _Summariser()
    loop = _loop(_Member("dear", ["text"]), summariser)
    conversation = _conversation(12)  # past compact_every: this turn compacts

    log, final = await _turn(loop, conversation)

    assert summariser.calls == 1
    assert log.history_compacted is True
    # 20.0 cents for the answer at ``vendor/dear`` plus 2.0 for the summary at
    # ``vendor/cheap``. Before R9-299 the turn was priced at 20.0 and the summary free.
    assert loop.last_turn_cost_cents == pytest.approx(22.0)
    assert log.cost_cents == pytest.approx(22.0)
    # The TurnLog row is the billing record: its tokens are the tokens it was priced on.
    assert (log.prompt_tokens, log.completion_tokens) == (
        _PROMPT + _SUMMARY_PROMPT,
        _COMPLETION + _SUMMARY_COMPLETION,
    )
    # The turn is still named by the model that answered, and the stream's usage is
    # still the answer's own.
    assert log.model_name == "dear"
    assert final.usage is not None
    assert (final.usage.prompt_tokens, final.usage.completion_tokens) == (_PROMPT, _COMPLETION)


async def test_a_turn_that_does_not_compact_is_priced_as_before() -> None:
    summariser = _Summariser()
    loop = _loop(_Member("dear", ["text"]), summariser)

    log, _final = await _turn(loop, _conversation(4))

    assert summariser.calls == 0
    assert log.history_compacted is False
    assert loop.last_turn_cost_cents == pytest.approx(20.0)
    assert (log.prompt_tokens, log.completion_tokens) == (_PROMPT, _COMPLETION)


async def test_a_turn_that_ends_at_a_gate_after_compacting_carries_the_compaction() -> None:
    """A gate (here: confirming a pending initiative proposal) ends the turn before any
    answer round, with no TurnLog. The compaction already ran, so ``last_turn_cost_cents``
    carries its cost: the chat worker then charges it proportionally instead of the
    flat floor it charges a turn with no recorded cost."""
    summariser = _Summariser()
    answer = _Member("dear", [])  # never reached: the gate answers
    loop = _loop(answer, summariser, initiative_pending_provider=lambda: "notice-1")

    chunks = [chunk async for chunk in loop.turn(_conversation(12), "yes")]

    assert chunks[-1].is_final
    assert summariser.calls == 1
    assert loop.last_turn_cost_cents == pytest.approx(2.0)  # the summary at vendor/cheap
    assert loop.last_turn_cost_basis == "estimate_static"


async def test_a_turn_that_ends_at_a_gate_without_compacting_records_no_cost() -> None:
    """Unchanged: no compaction, no model call, no recorded cost (the flat floor)."""
    loop = _loop(_Member("dear", []), _Summariser(), initiative_pending_provider=lambda: "n1")

    _ = [chunk async for chunk in loop.turn(_conversation(4), "yes")]

    assert loop.last_turn_cost_cents is None
