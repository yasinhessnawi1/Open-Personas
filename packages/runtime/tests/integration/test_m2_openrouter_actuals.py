"""M2-T3 — the OpenRouter response-side actual through the REAL loop (D-M2-3).

Drives real :class:`ConversationLoop` turns (scripted backends, no network):

* an OpenRouter-flavoured backend whose final chunk carries
  ``TokenUsage.cost_usd`` → the TurnLog records the ACTUAL
  (basis ``actual_openrouter``, ``cost_cents == round(usd * 100, 6)``),
* the same actual SURVIVES a genuine :class:`MultiModelChatBackend` fallback
  (the wrapper re-yields the winner's chunks — carriage is proven on the real
  chain, composing with T2's served-model attribution), and
* an OpenRouter turn WITHOUT an actual degrades honestly (the bare loop's
  static-only chain misses the catalog slug → ``unpriced``, cost 0.0 — the
  absent-data invariant).

``@pytest.mark.integration`` — excluded from the default run.
"""

# ruff: noqa: SLF001, ARG002
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _fakes import FakeStore  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import StreamChunk, TokenUsage
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends import ChatBackend
    from persona.backends.types import ToolSpec
    from persona.schema.conversation import ConversationMessage

pytestmark = pytest.mark.integration

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


class _OpenRouterScripted:
    """An OpenRouter-flavoured ChatBackend double.

    Streams one text chunk then a final chunk whose ``TokenUsage`` carries the
    response-side actual (``cost_usd``) — the shape ``_stream_openai`` emits
    for a real OpenRouter response with usage accounting opted in.
    """

    def __init__(self, *, model: str, cost_usd: float | None) -> None:
        self._model = model
        self._cost_usd = cost_usd

    @property
    def provider_name(self) -> str:
        return "openrouter"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **kwargs: object) -> object:
        raise NotImplementedError  # the loop streams

    async def chat_stream(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        **kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(delta="routed reply")
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cost_usd=self._cost_usd,
            ),
        )


class _FailingBackend:
    """A primary that genuinely 429s inside its stream (the T2 double)."""

    def __init__(self, *, provider: str, model: str) -> None:
        self._provider = provider
        self._model = model
        self.stream_calls = 0

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **kwargs: object) -> object:
        raise RateLimitError(
            "scripted 429",
            context={"provider": self._provider, "status_code": "429"},
        )

    async def chat_stream(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        **kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        raise RateLimitError(
            "scripted 429",
            context={"provider": self._provider, "status_code": "429"},
        )
        yield  # pragma: no cover — makes this an async generator


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="A", role="r", background="b", constraints=["c"]),
    )


def _make_loop(backend: ChatBackend) -> tuple[ConversationLoop, MemoryTurnLogWriter]:
    toolbox = Toolbox([], allow_list=None)  # type: ignore[arg-type]
    registry = TierRegistry({"mid": TierConfig(name="mid", backend_config=_DUMMY_CFG)})
    registry._cache = {"mid": backend}  # type: ignore[assignment]
    writer = MemoryTurnLogWriter()
    loop = ConversationLoop(
        persona=_persona(),
        stores={k: FakeStore() for k in ("identity", "self_facts", "worldview", "episodic")},  # type: ignore[arg-type]
        toolbox=toolbox,
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        turn_log_writer=writer,
    )
    return loop, writer


def _conv() -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=[])


@pytest.mark.asyncio
async def test_openrouter_actual_prices_the_turn() -> None:
    backend = _OpenRouterScripted(model="z-ai/glm-4.6", cost_usd=0.00042)
    loop, writer = _make_loop(backend)  # type: ignore[arg-type]

    _ = [c async for c in loop.turn(_conv(), "hello")]

    log = writer.logs[-1]
    assert log.cost_basis == "actual_openrouter"
    # 0.00042 USD → 0.042 cents (micro-cent rounding).
    assert log.cost_cents == pytest.approx(0.042)
    assert log.model_name == "z-ai/glm-4.6"
    assert log.provider == "openrouter"


@pytest.mark.asyncio
async def test_free_route_zero_actual_is_recorded_as_actual() -> None:
    # A ``:free`` route's actual IS 0.0 — recorded as an actual, not unpriced.
    backend = _OpenRouterScripted(model="some/free-model:free", cost_usd=0.0)
    loop, writer = _make_loop(backend)  # type: ignore[arg-type]

    _ = [c async for c in loop.turn(_conv(), "hello")]

    log = writer.logs[-1]
    assert log.cost_basis == "actual_openrouter"
    assert log.cost_cents == 0.0


@pytest.mark.asyncio
async def test_actual_survives_a_real_fallback() -> None:
    """Wrapper carriage: the winner's cost_usd rides the re-yielded chunks."""
    primary = _FailingBackend(provider="anthropic", model="claude-sonnet-4-6")
    secondary = _OpenRouterScripted(model="z-ai/glm-4.6", cost_usd=0.00042)
    wrapper = MultiModelChatBackend(
        [primary, secondary],  # type: ignore[list-item]
        tier_name="mid",
        max_retries_per_backend=0,
    )
    loop, writer = _make_loop(wrapper)  # type: ignore[arg-type]

    _ = [c async for c in loop.turn(_conv(), "hello")]

    assert primary.stream_calls == 1  # the primary genuinely ran and failed
    log = writer.logs[-1]
    # T3 (actuals) composes with T2 (served attribution) on the real chain.
    assert log.cost_basis == "actual_openrouter"
    assert log.cost_cents == pytest.approx(0.042)
    assert log.model_name == "z-ai/glm-4.6"
    assert log.provider == "openrouter"
    assert log.fallback_engaged is True
    assert log.tier_fallback_reasons == ["RateLimitError"]


@pytest.mark.asyncio
async def test_absent_actual_degrades_honestly() -> None:
    # No actual + a catalog slug the bare loop's static-only chain cannot
    # price → unpriced, cost 0.0 (the absent-data invariant; never a guess).
    backend = _OpenRouterScripted(model="z-ai/glm-4.6", cost_usd=None)
    loop, writer = _make_loop(backend)  # type: ignore[arg-type]

    _ = [c async for c in loop.turn(_conv(), "hello")]

    log = writer.logs[-1]
    assert log.cost_basis == "unpriced"
    assert log.cost_cents == 0.0
