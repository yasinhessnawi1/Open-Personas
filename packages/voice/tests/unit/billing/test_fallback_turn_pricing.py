"""A voice turn a fallback answered is charged at the model that served it (R9-221).

Driven through a REAL :class:`VoiceModelReplyProducer` over a REAL
:class:`MultiModelChatBackend` whose primary genuinely raises a 429 inside
``chat_stream``, feeding a REAL :class:`VoiceTurnBillingMeter` whose deduct lands on a
recording ledger. The assertion is on the ``cost_cents`` the ledger row records, the
number the charge is made from.

Every model in these chains has its own registry rate in an injected cost source, and
each rate prices the scripted usage to a different number, so an assertion names which
model's price the turn was charged. STT and TTS are configured as providers with no
priced row, so the recorded cost is the model arm alone.
"""

# Test doubles keep the loose signatures of the protocols they stand in for.
# ruff: noqa: ANN401, ARG002
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import pytest
from persona.backends import BackendConfig, StreamChunk, TokenUsage
from persona.backends.errors import RateLimitError
from persona.backends.model_metadata import ModelMetadata
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import ToolCallDelta
from persona.billing import BillingConfig
from persona.history import ConversationHistoryManager
from persona.schema.chunks import PersonaChunk
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity, RoutingConfig
from persona.schema.tools import ToolResult
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry
from persona_voice.billing import VoiceTurnBillingMeter
from persona_voice.loop.streaming import Transcript
from persona_voice.model import VoiceModelReplyProducer, VoiceTurnContext

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

pytestmark = pytest.mark.asyncio

#: Every scripted round reports this usage. At the rates below it prices to a
#: distinct number per model.
_PROMPT_TOKENS = 1000
_COMPLETION_TOKENS = 500
#: What an OpenRouter route reports it charged for the round, in USD.
_ACTUAL_USD = 0.0123
_ACTUAL_CENTS = 1.23


def _rate(input_cents_per_1k: float, output_cents_per_1k: float) -> ModelMetadata:
    return ModelMetadata(
        cost_input_per_1k_tokens=input_cents_per_1k,
        cost_output_per_1k_tokens=output_cents_per_1k,
        latency_p50_ms=100.0,
        quality_benchmark=0.5,
        tools_supported=True,
        vision_supported=False,
        context_length=8192,
    )


#: Registry rates keyed by the canonical id the cost function looks up: an OpenRouter
#: model is its slug, any other model is ``provider/model``.
_RATES = {
    "nvidia/primary-model": _rate(1.0, 2.0),  # 1000 in, 500 out -> 2.0 cents
    "vendor/primary-model": _rate(3.0, 6.0),  # -> 6.0 cents
    "anthropic/fallback-model": _rate(10.0, 20.0),  # -> 20.0 cents
    "vendor/fallback-model": _rate(30.0, 60.0),  # -> 60.0 cents
}
_PRICED = {
    "nvidia/primary-model": 2.0,
    "vendor/primary-model": 6.0,
    "anthropic/fallback-model": 20.0,
    "vendor/fallback-model": 60.0,
}


class _RateTable:
    """A ``CostSource`` over :data:`_RATES`, answering as the static table would."""

    def resolve_with_source(
        self, model_id: str, *, allow_fetch: bool = True
    ) -> tuple[ModelMetadata, Literal["static", "catalog"]] | None:
        metadata = _RATES.get(model_id)
        return None if metadata is None else (metadata, "static")


class _Speaker:
    """Answers every call with one sentence, then a final chunk carrying usage."""

    supports_native_tools = False
    supports_vision = False

    def __init__(self, provider: str, model: str, *, cost_usd: float | None) -> None:
        self.provider_name = provider
        self.model_name = model
        self._cost_usd = cost_usd
        self.stream_calls = 0

    async def chat_stream(self, messages: list[Any], **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        yield StreamChunk(delta=f"Hi from {self.model_name}. ")
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=_PROMPT_TOKENS,
                completion_tokens=_COMPLETION_TOKENS,
                total_tokens=_PROMPT_TOKENS + _COMPLETION_TOKENS,
                cost_usd=self._cost_usd,
            ),
        )


class _RateLimitedPrimary(_Speaker):
    """A primary whose stream raises a 429 before its first chunk, as a real one does."""

    def __init__(self, provider: str, model: str) -> None:
        super().__init__(provider, model, cost_usd=None)

    async def chat_stream(self, messages: list[Any], **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        raise RateLimitError("scripted 429", context={"provider": self.provider_name})
        yield StreamChunk(delta="")  # pragma: no cover - makes this an async generator


class _ToolCallThenRateLimited(_Speaker):
    """Serves the first round as ONLY a tool call, then is rate limited on the follow-up."""

    async def chat_stream(self, messages: list[Any], **_kwargs: Any) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        if self.stream_calls > 1:
            raise RateLimitError("scripted 429", context={"provider": self.provider_name})
        yield StreamChunk(
            delta="",
            tool_call_delta=ToolCallDelta(
                call_id="c1", name_delta="web_search", arguments_delta='{"query": "rights"}'
            ),
        )
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=_PROMPT_TOKENS,
                completion_tokens=_COMPLETION_TOKENS,
                total_tokens=_PROMPT_TOKENS + _COMPLETION_TOKENS,
                cost_usd=self._cost_usd,
            ),
        )


@tool(name="web_search", description="Search the web.")
async def _web_search(query: str) -> ToolResult:
    return ToolResult(tool_name="web_search", content=f"results for {query}")


class _Store:
    def query(self, persona_id: str, query: str, top_k: int, **filters: Any) -> list[PersonaChunk]:
        return []

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return [
            PersonaChunk(id="id-1", text="I am Astrid.", metadata={}, created_at=datetime.now(UTC))
        ]

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return []


class _Engine:
    def dispose(self) -> None: ...


class _RecordingLedger:
    """A ``LedgerPort`` double recording every idempotent capture."""

    def __init__(self) -> None:
        self.captures: list[dict[str, object]] = []

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        self.captures.append(kw)
        amount = kw["amount"]
        assert isinstance(amount, int)
        return amount, 10_000


def _meter(ledger: _RecordingLedger) -> VoiceTurnBillingMeter:
    return VoiceTurnBillingMeter(
        ledger=ledger,  # type: ignore[arg-type]
        billing_config=BillingConfig(),
        engine_factory=_Engine,  # type: ignore[arg-type]
        user_id="owner-1",
        call_id="call1",
        # No priced row for either: the recorded cost is the model arm alone.
        stt_provider="no-priced-stt",
        stt_model="none",
        tts_provider="no-priced-tts",
        tts_model="none",
        streamed_seconds_reader=lambda: 0.0,
        cost_source=_RateTable(),
    )


def _producer(
    chain: MultiModelChatBackend,
    meter: VoiceTurnBillingMeter,
    toolbox: Toolbox | None = None,
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
            toolbox=toolbox,
        ),
        turn_meter=meter,
    )


async def _charged_cents(
    primary: _Speaker, fallback: _Speaker, toolbox: Toolbox | None = None
) -> tuple[float, str]:
    """Run one billed turn over ``[primary, fallback]``; return the recorded cost and speech."""
    chain = MultiModelChatBackend(
        [primary, fallback],  # type: ignore[list-item]
        tier_name="frontier",
        max_retries_per_backend=0,
    )
    ledger = _RecordingLedger()
    meter = _meter(ledger)
    stream = await _producer(chain, meter, toolbox)(
        Transcript(is_final=True, text="what are my rights?", confidence=1.0)
    )
    spoken = "".join([token async for token in stream])
    await meter.bill_turn(1)
    assert len(ledger.captures) == 1, ledger.captures
    cost_cents = ledger.captures[0]["cost_cents"]
    assert isinstance(cost_cents, float)
    return cost_cents, spoken


async def test_a_fallback_turn_with_an_openrouter_actual_cost_is_charged_that_actual_cost() -> None:
    # The primary is not an OpenRouter route, so billing it as the primary threw the
    # fallback's reported actual away and charged the primary's registry rate (2.0).
    primary = _RateLimitedPrimary("nvidia", "primary-model")
    fallback = _Speaker("openrouter", "vendor/fallback-model", cost_usd=_ACTUAL_USD)

    cost_cents, spoken = await _charged_cents(primary, fallback)

    assert spoken == "Hi from vendor/fallback-model. "
    assert primary.stream_calls == 1
    assert cost_cents == pytest.approx(_ACTUAL_CENTS)


@pytest.mark.parametrize(
    ("primary_pair", "fallback_pair", "fallback_cost_usd", "fallback_rate_id"),
    [
        pytest.param(
            ("openrouter", "vendor/primary-model"),
            ("anthropic", "fallback-model"),
            None,
            "anthropic/fallback-model",
            id="direct_fallback_behind_an_openrouter_primary",
        ),
        pytest.param(
            ("nvidia", "primary-model"),
            ("openrouter", "vendor/fallback-model"),
            None,
            "vendor/fallback-model",
            id="openrouter_fallback_that_reported_no_cost",
        ),
        pytest.param(
            # A cost figure from a route that is not OpenRouter is not what was paid
            # (the M2 review gate), so it must not become the charge. Billing the turn
            # as its OpenRouter primary used to mint exactly that.
            ("openrouter", "vendor/primary-model"),
            ("anthropic", "fallback-model"),
            _ACTUAL_USD,
            "anthropic/fallback-model",
            id="non_openrouter_fallback_reporting_a_cost",
        ),
    ],
)
async def test_a_fallback_turn_without_an_openrouter_actual_is_charged_at_the_fallbacks_rate(
    primary_pair: tuple[str, str],
    fallback_pair: tuple[str, str],
    fallback_cost_usd: float | None,
    fallback_rate_id: str,
) -> None:
    primary = _RateLimitedPrimary(*primary_pair)
    fallback = _Speaker(*fallback_pair, cost_usd=fallback_cost_usd)

    cost_cents, spoken = await _charged_cents(primary, fallback)

    assert spoken == f"Hi from {fallback.model_name}. "
    assert primary.stream_calls == 1
    assert cost_cents == pytest.approx(_PRICED[fallback_rate_id])


@pytest.mark.parametrize(
    ("primary_pair", "primary_cost_usd", "expected_cents"),
    [
        pytest.param(
            ("openrouter", "vendor/primary-model"),
            _ACTUAL_USD,
            _ACTUAL_CENTS,
            id="openrouter_primary_with_an_actual",
        ),
        pytest.param(
            ("nvidia", "primary-model"),
            None,
            _PRICED["nvidia/primary-model"],
            id="direct_primary_at_its_rate",
        ),
    ],
)
async def test_a_turn_the_primary_answered_is_charged_as_the_primary(
    primary_pair: tuple[str, str], primary_cost_usd: float | None, expected_cents: float
) -> None:
    primary = _Speaker(*primary_pair, cost_usd=primary_cost_usd)
    fallback = _Speaker("anthropic", "fallback-model", cost_usd=None)

    cost_cents, spoken = await _charged_cents(primary, fallback)

    assert spoken == f"Hi from {primary.model_name}. "
    assert fallback.stream_calls == 0
    assert cost_cents == pytest.approx(expected_cents)


@pytest.mark.parametrize(
    ("primary_pair", "primary_cost_usd", "expected_cents"),
    [
        pytest.param(
            ("nvidia", "primary-model"),
            None,
            _PRICED["nvidia/primary-model"] + _PRICED["anthropic/fallback-model"],
            id="both_rounds_at_their_own_rates",
        ),
        pytest.param(
            ("openrouter", "vendor/primary-model"),
            _ACTUAL_USD,
            _ACTUAL_CENTS + _PRICED["anthropic/fallback-model"],
            id="an_openrouter_actual_round_then_a_rated_round",
        ),
    ],
)
async def test_each_round_of_a_tool_turn_is_charged_at_the_model_that_served_it(
    primary_pair: tuple[str, str], primary_cost_usd: float | None, expected_cents: float
) -> None:
    # Round 1 is the primary's tool call; the follow-up's primary attempt is rate limited
    # and the fallback speaks. Each round is priced at its own served model. Pricing the
    # summed tokens at the last round's model would charge 40.0 here, twice the fallback's
    # rate for tokens the primary produced.
    primary = _ToolCallThenRateLimited(*primary_pair, cost_usd=primary_cost_usd)
    fallback = _Speaker("anthropic", "fallback-model", cost_usd=None)

    cost_cents, spoken = await _charged_cents(
        primary,
        fallback,
        Toolbox([_web_search], allow_list=None),  # type: ignore[list-item]
    )

    assert primary.stream_calls == 2
    assert fallback.stream_calls == 1
    assert spoken.endswith("Hi from fallback-model. ")
    assert cost_cents == pytest.approx(expected_cents, abs=1e-9)
