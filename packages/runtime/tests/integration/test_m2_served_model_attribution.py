"""M2-T2 — served-model attribution through the REAL fallback chain (D-M2-2).

Pre-M2, a fallback-engaged turn's ``TurnLog.model_name`` / ``provider`` /
``cost_cents`` carried the multi-model wrapper's PRIMARY (never-invoked) model.
These tests drive the REAL trigger chain — a genuine
:class:`MultiModelChatBackend` whose primary genuinely raises inside
``chat_stream`` (no hand-forced attempt ledger, the A4/V8 rule) — through a
real :class:`ConversationLoop` turn, and assert:

* the turn is NAMED as the served (secondary) model,
* the turn is PRICED at the served model's rate — not the primary's,
* the ``tier_*`` projection and the named identity agree, and
* primary-only turns (bare backend AND wrapper-primary-serves) are
  byte-identical to the pre-M2 shape.

Harness mirrors ``test_spec25_turnlog_sweep.py`` (scripted fakes, no network);
``@pytest.mark.integration`` — excluded from the default run.
"""

# ruff: noqa: SLF001, ARG002
from __future__ import annotations

from typing import TYPE_CHECKING

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
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends import ChatBackend, StreamChunk
    from persona.backends.types import ToolSpec
    from persona.schema.conversation import ConversationMessage

pytestmark = pytest.mark.integration

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]

# Authoritative static-table rates (cents/1k) x the ScriptedBackend's fixed
# final-chunk usage (prompt=10, completion=5).
_DEEPSEEK_COST = (10 / 1000.0) * 0.027 + (5 / 1000.0) * 0.11
_ANTHROPIC_COST = (10 / 1000.0) * 0.30 + (5 / 1000.0) * 1.50


class _FailingBackend:
    """A ChatBackend double whose stream GENUINELY raises pre-first-chunk.

    The raise happens inside the async generator body (the real place a
    provider 429 surfaces), so the wrapper's own classifier and attempt
    ledger do the work — nothing is hand-forced.
    """

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
async def test_fallback_turn_names_and_prices_the_served_model() -> None:
    """The REAL chain: primary 429s inside its stream; secondary serves."""
    primary = _FailingBackend(provider="anthropic", model="claude-sonnet-4-6")
    secondary = ScriptedBackend(
        [ScriptedRound(text="served by fallback")],
        provider_name="deepseek",
        model_name="deepseek-chat",
    )
    wrapper = MultiModelChatBackend(
        [primary, secondary],  # type: ignore[list-item]
        tier_name="mid",
        max_retries_per_backend=0,  # no same-model retry: one attempt, one fallthrough
    )
    loop, writer = _make_loop(wrapper)  # type: ignore[arg-type]

    _ = [c async for c in loop.turn(_conv(), "hello")]

    assert primary.stream_calls == 1  # the primary genuinely ran and failed
    log = writer.logs[-1]
    # D-M2-2: named as the SERVED model (pre-M2 these said anthropic/sonnet).
    assert log.model_name == "deepseek-chat"
    assert log.provider == "deepseek"
    # Priced at the served model's authoritative static rate...
    assert log.cost_cents == pytest.approx(_DEEPSEEK_COST)
    assert log.cost_basis == "estimate_static"
    # ...and NOT at the never-invoked primary's rate (the pre-M2 bug).
    assert log.cost_cents != pytest.approx(_ANTHROPIC_COST)
    # The two identity families now agree.
    assert log.tier_model_chosen == log.model_name
    assert log.tier_provider_used == log.provider
    # The ledger is the wrapper's own — real classifier output, not forced.
    assert log.fallback_engaged is True
    assert log.tier_fallback_count == 1
    assert log.tier_fallback_reasons == ["RateLimitError"]
    assert log.tier_fallback_providers == ["anthropic"]


@pytest.mark.asyncio
async def test_primary_only_bare_backend_is_byte_identical() -> None:
    """A bare (non-wrapper) backend turn: the additive invariant."""
    backend = ScriptedBackend(
        [ScriptedRound(text="hi")], provider_name="anthropic", model_name="claude-sonnet-4-6"
    )
    loop, writer = _make_loop(backend)  # type: ignore[arg-type]

    _ = [c async for c in loop.turn(_conv(), "hello")]

    log = writer.logs[-1]
    assert log.model_name == "claude-sonnet-4-6"
    assert log.provider == "anthropic"
    assert log.cost_cents == pytest.approx(_ANTHROPIC_COST)
    assert log.cost_basis == "estimate_static"
    assert log.fallback_engaged is False
    assert log.tier_fallback_count == 0
    # Identity agreement holds on the no-fallback shape too.
    assert log.tier_model_chosen == log.model_name
    assert log.tier_provider_used == log.provider


@pytest.mark.asyncio
async def test_wrapper_primary_serves_names_the_primary() -> None:
    """Wrapper composed, primary healthy: served == primary, no fallback."""
    primary = ScriptedBackend(
        [ScriptedRound(text="hi")], provider_name="anthropic", model_name="claude-sonnet-4-6"
    )
    secondary = ScriptedBackend(
        [ScriptedRound(text="never used")],
        provider_name="deepseek",
        model_name="deepseek-chat",
    )
    wrapper = MultiModelChatBackend(
        [primary, secondary],  # type: ignore[list-item]
        tier_name="mid",
        max_retries_per_backend=0,
    )
    loop, writer = _make_loop(wrapper)  # type: ignore[arg-type]

    _ = [c async for c in loop.turn(_conv(), "hello")]

    log = writer.logs[-1]
    assert log.model_name == "claude-sonnet-4-6"
    assert log.provider == "anthropic"
    assert log.cost_cents == pytest.approx(_ANTHROPIC_COST)
    assert log.fallback_engaged is False
    assert log.tier_model_chosen == "claude-sonnet-4-6"
    assert log.tier_provider_used == "anthropic"
