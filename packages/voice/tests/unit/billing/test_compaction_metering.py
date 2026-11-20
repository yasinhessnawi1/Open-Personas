"""Voice history compaction is billed to the caller (R9-299, D-M3-1).

A long call folds its older turns into a running summary between turns, with a model
call on the small tier (``make_small_tier_summariser``, scheduled off the audio path by
the turn recorder). Before R9-299 that call reported its usage to nobody: the per-turn
meter only heard the reply's own rounds, so every compaction was a provider call the
caller was never charged for.

The summary now reports its usage to the session's turn meter, which bills it with the
next committed turn, priced at the model that served it like any other round.
"""

# ruff: noqa: ANN401, ARG002 - doubles with intentionally loose signatures.

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from persona.backends import BackendConfig
from persona.backends.types import BackendAttempt, ChatResponse, TokenUsage
from persona.billing import BillingConfig
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.billing import VoiceTurnBillingMeter
from persona_voice.model import (
    VoiceHistoryCompactor,
    VoiceTurnContext,
    VoiceTurnRecorder,
    make_small_tier_summariser,
)
from persona_voice.turn_taking.heard_words import BargedReply

#: The summary's usage. On OpenRouter the response carries its own actual cost, so the
#: price is the provider's own figure: 0.004 USD is 0.4 cents.
_SUMMARY_COST_USD = 0.004


class _Summary:
    """The small tier's backend: one non-streaming call with its own usage."""

    provider_name = "openrouter"
    model_name = "vendor/small"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Any, **_kwargs: Any) -> ChatResponse:
        self.calls += 1
        return ChatResponse(
            content="  they agreed on Tuesday  ",
            tool_calls=[],
            usage=TokenUsage(
                prompt_tokens=600,
                completion_tokens=80,
                total_tokens=680,
                cost_usd=_SUMMARY_COST_USD,
            ),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=1.0,
        )


class _Registry:
    def __init__(self, backend: _Summary) -> None:
        self._backend = backend

    def get(self, tier: str) -> _Summary:
        assert tier == "small"
        return self._backend


class _UsageListener:
    def __init__(self) -> None:
        self.rounds: list[dict[str, Any]] = []

    def note_llm_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
        provider: str,
        model: str,
        failed_tries: tuple[BackendAttempt, ...] = (),
    ) -> None:
        self.rounds.append(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": cost_usd,
                "provider": provider,
                "model": model,
                "failed_tries": failed_tries,
            }
        )


class _Store:
    def write(self, persona_id: str, chunks: list[PersonaChunk], **kwargs: Any) -> None:
        return None

    def query(self, persona_id: str, query: str, top_k: int, **filters: Any) -> list[PersonaChunk]:
        return []

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return []


class _FakeEngine:
    def dispose(self) -> None:
        return None


class _RecordingLedger:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        self.calls.append(kw)
        amount = kw["amount"]
        assert isinstance(amount, int)
        return amount, 100


def _meter(ledger: _RecordingLedger) -> VoiceTurnBillingMeter:
    return VoiceTurnBillingMeter(
        ledger=ledger,  # type: ignore[arg-type]
        billing_config=BillingConfig(),
        engine_factory=_FakeEngine,  # type: ignore[arg-type]
        user_id="owner-1",
        call_id="call1",
        stt_provider="gladia",
        stt_model="solaria-1",
        tts_provider="elevenlabs",
        tts_model="eleven_flash_v2_5",
        streamed_seconds_reader=lambda: 0.0,
        floor=1,
    )


def _context(prior_messages: int) -> VoiceTurnContext:
    now = datetime.now(UTC)
    cfg = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
    return VoiceTurnContext(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(
                name="Astrid", role="assistant", background="x", constraints=[]
            ),
        ),
        stores={k: _Store() for k in ("identity", "self_facts", "worldview", "episodic")},  # type: ignore[misc]
        conversation=Conversation(
            conversation_id="c1",
            persona_id="astrid",
            messages=[
                ConversationMessage(role="user", content=f"t{i}", created_at=now)
                for i in range(prior_messages)
            ],
        ),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=TierRegistry({"frontier": TierConfig(name="frontier", backend_config=cfg)}),
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
    )


@pytest.mark.asyncio
async def test_the_small_tier_summary_reports_its_usage_at_the_model_that_served_it() -> None:
    backend = _Summary()
    summariser = make_small_tier_summariser(_Registry(backend))  # type: ignore[arg-type]
    listener = _UsageListener()
    summariser.set_usage_listener(listener)

    text = await summariser([])

    assert text == "they agreed on Tuesday"
    assert listener.rounds == [
        {
            "prompt_tokens": 600,
            "completion_tokens": 80,
            "cost_usd": _SUMMARY_COST_USD,
            "provider": "openrouter",
            "model": "vendor/small",
            "failed_tries": (),
        }
    ]


@pytest.mark.asyncio
async def test_a_summariser_with_no_listener_still_summarises() -> None:
    summariser = make_small_tier_summariser(_Registry(_Summary()))  # type: ignore[arg-type]

    assert await summariser([]) == "they agreed on Tuesday"


@pytest.mark.asyncio
async def test_a_compaction_is_billed_with_the_next_committed_turn() -> None:
    """Through the real recorder and the real meter: the turn that follows a compaction
    is charged for it. Turn 2 has no speech, no synthesis and no reply rounds of its own
    here, so before R9-299 it priced at zero and charged nothing at all."""
    backend = _Summary()
    ledger = _RecordingLedger()
    ctx = _context(prior_messages=16)  # compaction is due once the next turn lands
    scheduled: list[Any] = []
    recorder = VoiceTurnRecorder(
        ctx,
        compactor=VoiceHistoryCompactor(ctx.history_manager),
        summariser=make_small_tier_summariser(_Registry(backend)),  # type: ignore[arg-type]
        scheduler=scheduled.append,  # run the background compaction by hand
    )
    recorder.set_billing_meter(_meter(ledger))

    recorder.note_user_message("first")
    await recorder.on_reply_committed(BargedReply(heard_text="a", truncated=False, token_count=1))
    assert len(scheduled) == 1
    await asyncio.gather(*scheduled)  # the off-path compaction runs between turns
    assert backend.calls == 1

    recorder.note_user_message("second")
    await recorder.on_reply_committed(BargedReply(heard_text="b", truncated=False, token_count=1))
    for leftover in scheduled[1:]:
        leftover.close()

    (charge,) = ledger.calls
    assert charge["billing_key"] == "voice:call1:2"
    assert charge["cost_cents"] == pytest.approx(0.4)  # the summary's own actual
    assert charge["amount"] == 1  # 0.4 cents, ceiled, at the floor
