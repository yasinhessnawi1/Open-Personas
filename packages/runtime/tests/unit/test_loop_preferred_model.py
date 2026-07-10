"""Spec M1 T3 — the persona's preferred model preempts the scorer; tier chain is the fallback.

The persona's ``routing.preferred_model`` (once a ``preferred_backend_provider`` is wired
— T4 injects T2's OpenRouter passthrough builder) PREEMPTS the Spec 23 intelligent scorer:
the chosen id fronts the tier backend as the PRIMARY of a ``MultiModelChatBackend`` chain,
so a passthrough error falls through to the tier's own models (the chain IS the error
fallback). A capability gate skips a tools-incapable preferred model on a tools-requiring
turn (fail-open when metadata/router is unavailable).

Mirrors the ``ConversationLoop`` model-selection harness from ``test_loop_intelligent_routing.py``
(scripted backends, a ``MultiModelChatBackend`` tier, a map metadata resolver): the loop under
test is the SAME one that carries the seam (``persona_runtime.loop.ConversationLoop``), NOT the
seam-less ``AgenticLoop`` of ``test_loop_agentic.py``.

Scripted backends, no real model / network — lives in tests/unit/.
"""

# ruff: noqa: ANN401, ARG002 — test-double protocol methods accept-and-ignore loop kwargs.

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona import logging as plog
from persona.backends import BackendConfig
from persona.backends.errors import BackendTimeoutError
from persona.backends.model_metadata import ModelMetadata
from persona.backends.multi_model import MultiModelChatBackend
from persona.config import PersonaCoreConfig
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import (
    IntelligentRoutingConfig,
    Persona,
    PersonaIdentity,
    RoutingConfig,
)
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.routing import HeuristicRouter, IntelligentRouter
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from persona.backends.protocol import ChatBackend
    from persona.backends.types import ChatResponse, StreamChunk
    from persona.schema.conversation import ConversationMessage

_CFG = BackendConfig(provider="anthropic", model="primary", api_key="sk-test")
_PREFERRED_ID = "fake/preferred"


# ----- fakes / builders ----------------------------------------------------


class _MapResolver:
    """A minimal ModelMetadataResolver over a fixed id → metadata table."""

    def __init__(self, table: dict[str, ModelMetadata]) -> None:
        self._table = table

    def resolve(self, model_id: str) -> ModelMetadata | None:
        return self._table.get(model_id)


class _RaisingResolver:
    """A resolver that always raises — proves ``metadata_for`` fails open (⇒ None ⇒ ALLOW)."""

    def resolve(self, model_id: str) -> ModelMetadata | None:
        msg = "resolver boom"
        raise RuntimeError(msg)


class _RaisingBackend:
    """A ChatBackend whose stream raises a RETRYABLE provider error before its first chunk.

    Proves the composed ``MultiModelChatBackend`` chain engages: a retryable error on the
    fronted passthrough falls through (D-20-9 RETRY-THEN-FALLBACK) to the tier's own subs.
    """

    def __init__(self, *, provider_name: str, model_name: str) -> None:
        self._provider_name = provider_name
        self._model_name = model_name
        self.chat_stream_calls = 0

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_kwargs: Any) -> ChatResponse:
        raise BackendTimeoutError(
            "passthrough timed out",
            context={"provider": self._provider_name, "model": self._model_name},
        )

    async def chat_stream(
        self, messages: list[ConversationMessage], **_kwargs: Any
    ) -> AsyncIterator[StreamChunk]:
        self.chat_stream_calls += 1
        raise BackendTimeoutError(
            "passthrough timed out",
            context={"provider": self._provider_name, "model": self._model_name},
        )
        yield  # pragma: no cover — unreached; present only so this is an async generator


def _md(*, tools_supported: bool = True, quality: float = 0.9) -> ModelMetadata:
    return ModelMetadata(
        cost_input_per_1k_tokens=0.1,
        cost_output_per_1k_tokens=0.1,
        latency_p50_ms=300.0,
        quality_benchmark=quality,
        tools_supported=tools_supported,
        vision_supported=True,
        context_length=200_000,
    )


def _frontier_registry_with_subs() -> tuple[TierRegistry, list[ScriptedBackend]]:
    """A frontier tier backed by a 2-model MultiModel wrapper (cheap slot 0, good slot 1)."""
    subs = [
        ScriptedBackend(
            [ScriptedRound(text="cheap says hi")], provider_name="deepseek", model_name="cheap"
        ),
        ScriptedBackend(
            [ScriptedRound(text="good says hi")], provider_name="anthropic", model_name="good"
        ),
    ]
    wrapper = MultiModelChatBackend(subs, tier_name="frontier")  # type: ignore[arg-type]
    registry = TierRegistry(
        {
            "frontier": TierConfig(
                name="frontier", backend_config=_CFG, preconstructed_backend=wrapper
            )
        }
    )
    return registry, subs


def _persona(
    *, preferred_model: str | None = None, intelligent: IntelligentRoutingConfig | None = None
) -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="bg", constraints=[]),
        routing=RoutingConfig(
            preferred_model=preferred_model,
            intelligent=intelligent if intelligent is not None else IntelligentRoutingConfig(),
        ),
    )


def _provider_returning(backend: object) -> Callable[[str], ChatBackend | None]:
    """A preferred_backend_provider that returns ``backend`` for the preferred id, else None."""

    def _provider(model_id: str) -> ChatBackend | None:
        return backend if model_id == _PREFERRED_ID else None  # type: ignore[return-value]

    return _provider


def _build_loop(
    *,
    persona: Persona,
    registry: TierRegistry,
    preferred_backend_provider: Callable[[str], ChatBackend | None] | None = None,
    intelligent_router: IntelligentRouter | None = None,
) -> tuple[ConversationLoop, MemoryTurnLogWriter]:
    writer = MemoryTurnLogWriter()
    loop = ConversationLoop(
        persona=persona,
        stores={k: FakeStore() for k in ("identity", "self_facts", "worldview", "episodic")},  # type: ignore[arg-type, misc]
        toolbox=Toolbox([], allow_list=None),  # type: ignore[arg-type]
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=HeuristicRouter(tier_registry=registry),
        tier_registry=registry,
        turn_log_writer=writer,
        intelligent_router=intelligent_router,
        preferred_backend_provider=preferred_backend_provider,
    )
    return loop, writer


def _conversation() -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=[])


# ----- tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_preferred_model_is_byte_identical() -> None:
    # No preferred_model + no provider ⇒ the tier system routes exactly as before:
    # every model-selection field stays at its default and the tier's OWN primary
    # (slot 0) serves the turn — no passthrough is fronted (the M1 seam is inert).
    registry, subs = _frontier_registry_with_subs()
    loop, writer = _build_loop(persona=_persona(), registry=registry)

    chunks = [c async for c in loop.turn(_conversation(), "hello")]
    text = "".join(c.delta for c in chunks)

    assert chunks[-1].is_final is True
    assert "cheap says hi" in text  # tier primary served it, unfronted
    assert subs[0].chat_stream_calls == 1
    assert subs[1].chat_stream_calls == 0
    # The routing decision is byte-identical to the pre-M1 baseline (all defaults).
    d = writer.logs[0].routing_decision
    assert d is not None
    assert d.model_candidates == ()
    assert d.score_vector == {}
    assert d.weights_used == {}
    assert d.model_fallback_engaged is False
    assert d.model_fallback_reason is None


@pytest.mark.asyncio
async def test_preferred_model_preempts_scorer_and_fronts_backend() -> None:
    # preferred_model set (with surrounding whitespace — proving the T1 strip) + a
    # provider returning a passthrough named ("openrouter", "fake/preferred"). An
    # ENABLED IntelligentRouter is also wired whose scorer would pick a DIFFERENT model;
    # the preferred model must PREEMPT it. One turn: the FIRST (and only) backend
    # attempted is the passthrough, and the decision carries the preferred provenance.
    registry, subs = _frontier_registry_with_subs()
    passthrough = ScriptedBackend(
        [ScriptedRound(text="preferred says hi")],
        provider_name="openrouter",
        model_name=_PREFERRED_ID,
    )
    resolver = _MapResolver(
        {"anthropic/good": _md(quality=0.99), "deepseek/cheap": _md(quality=0.10)}
    )
    loop, writer = _build_loop(
        persona=_persona(
            preferred_model=f"  {_PREFERRED_ID}  ",
            intelligent=IntelligentRoutingConfig(enabled=True),
        ),
        registry=registry,
        preferred_backend_provider=_provider_returning(passthrough),
        intelligent_router=IntelligentRouter(tier_registry=registry, metadata_resolver=resolver),
    )

    chunks = [c async for c in loop.turn(_conversation(), "hello")]
    text = "".join(c.delta for c in chunks)

    # The passthrough was the FIRST backend attempted and served the turn...
    assert passthrough.chat_stream_calls == 1
    assert "preferred says hi" in text
    # ...and the tier's own models were never reached (preferred fronted the chain).
    assert subs[0].chat_stream_calls == 0
    assert subs[1].chat_stream_calls == 0
    # Provenance: the STRIPPED preferred id + the preferred_model reason; the scorer was
    # skipped, so its candidate list never populated (proves preemption over Spec 23).
    d = writer.logs[0].routing_decision
    assert d is not None
    assert d.model == _PREFERRED_ID
    assert d.model_fallback_reason == "preferred_model"
    assert d.model_candidates == ()


@pytest.mark.asyncio
async def test_preferred_model_error_falls_to_tier_chain() -> None:
    # The fronted passthrough raises a RETRYABLE provider error before its first chunk;
    # the composed MultiModelChatBackend chain must fall through to the tier's own model
    # so the turn still completes (the chain IS the error fallback). No intelligent router
    # — the preferred hook works independently of Spec 23.
    registry, subs = _frontier_registry_with_subs()
    raiser = _RaisingBackend(provider_name="openrouter", model_name=_PREFERRED_ID)
    loop, writer = _build_loop(
        persona=_persona(preferred_model=_PREFERRED_ID),
        registry=registry,
        preferred_backend_provider=_provider_returning(raiser),
    )

    chunks = [c async for c in loop.turn(_conversation(), "hello")]
    text = "".join(c.delta for c in chunks)

    assert chunks[-1].is_final is True
    # The passthrough was attempted (and raised)...
    assert raiser.chat_stream_calls >= 1
    # ...and the tier's primary served the completion (the chain engaged).
    assert subs[0].chat_stream_calls >= 1
    assert "cheap says hi" in text
    # Provenance still records the preferred choice (the id the turn ASKED for).
    d = writer.logs[0].routing_decision
    assert d is not None
    assert d.model_fallback_reason == "preferred_model"


@pytest.mark.asyncio
async def test_capability_gate_tools_turn_skips_incapable_preferred() -> None:
    # metadata_for reports tools_supported=False for the preferred model. A tools-requiring
    # turn must gate it OUT (route to the tier default); a plain text turn must still use it.
    # NB: the ConversationLoop's _build_routing_context pins requires_strong_tools=False (a
    # documented v0.1 default), so the tools-requiring turn is exercised through the REAL gate
    # method with a tools-flagged RoutingContext — the closest faithful assertion (see report).
    registry, _subs = _frontier_registry_with_subs()
    passthrough = ScriptedBackend(
        [ScriptedRound(text="preferred says hi")],
        provider_name="openrouter",
        model_name=_PREFERRED_ID,
    )
    resolver = _MapResolver({_PREFERRED_ID: _md(tools_supported=False)})
    loop, writer = _build_loop(
        persona=_persona(
            preferred_model=_PREFERRED_ID, intelligent=IntelligentRoutingConfig(enabled=True)
        ),
        registry=registry,
        preferred_backend_provider=_provider_returning(passthrough),
        intelligent_router=IntelligentRouter(tier_registry=registry, metadata_resolver=resolver),
    )

    plain_ctx = loop._build_routing_context("hello", _conversation(), turn_has_image=False)
    tools_ctx = plain_ctx.model_copy(update={"requires_strong_tools": True})

    # A tools-requiring turn: the incapable preferred model is gated OUT (→ tier default).
    assert loop._preferred_model_for_turn(tools_ctx) is None
    # A plain text turn: the preferred model is still used.
    assert loop._preferred_model_for_turn(plain_ctx) == _PREFERRED_ID

    # End-to-end plain-text turn still routes to (and fronts) the preferred model.
    chunks = [c async for c in loop.turn(_conversation(), "hello")]
    assert "preferred says hi" in "".join(c.delta for c in chunks)
    assert passthrough.chat_stream_calls == 1
    d = writer.logs[0].routing_decision
    assert d is not None
    assert d.model_fallback_reason == "preferred_model"


@pytest.mark.asyncio
async def test_capability_gate_fails_open_when_metadata_unavailable() -> None:
    # Fail-open contract: on a tools-requiring turn, a resolver that RAISES (metadata_for
    # swallows ⇒ None) OR no router at all ⇒ ALLOW the preferred model (the runtime
    # tier-chain still guards). This is the safety the gate must never over-reach.
    registry, _subs = _frontier_registry_with_subs()
    passthrough = ScriptedBackend(
        [ScriptedRound(text="hi")], provider_name="openrouter", model_name=_PREFERRED_ID
    )
    provider = _provider_returning(passthrough)

    # (a) resolver raises ⇒ metadata_for None ⇒ ALLOW.
    loop_raise, _ = _build_loop(
        persona=_persona(
            preferred_model=_PREFERRED_ID, intelligent=IntelligentRoutingConfig(enabled=True)
        ),
        registry=registry,
        preferred_backend_provider=provider,
        intelligent_router=IntelligentRouter(
            tier_registry=registry, metadata_resolver=_RaisingResolver()
        ),
    )
    tools_ctx = loop_raise._build_routing_context(
        "hello", _conversation(), turn_has_image=False
    ).model_copy(update={"requires_strong_tools": True})
    assert loop_raise._preferred_model_for_turn(tools_ctx) == _PREFERRED_ID

    # (b) no IntelligentRouter at all ⇒ the gate has nothing to consult ⇒ ALLOW.
    loop_norouter, _ = _build_loop(
        persona=_persona(preferred_model=_PREFERRED_ID),
        registry=registry,
        preferred_backend_provider=provider,
    )
    tools_ctx2 = loop_norouter._build_routing_context(
        "hello", _conversation(), turn_has_image=False
    ).model_copy(update={"requires_strong_tools": True})
    assert loop_norouter._preferred_model_for_turn(tools_ctx2) == _PREFERRED_ID


def test_metadata_for_resolves_and_fails_open() -> None:
    # The new IntelligentRouter.metadata_for helper: resolves a hit, returns None on a
    # miss, and swallows a raising resolver (fail-open ⇒ None) so the gate never crashes.
    registry, _subs = _frontier_registry_with_subs()
    md = _md(tools_supported=False)
    router = IntelligentRouter(
        tier_registry=registry, metadata_resolver=_MapResolver({_PREFERRED_ID: md})
    )
    assert router.metadata_for(_PREFERRED_ID) is md
    assert router.metadata_for("unknown/model") is None
    router_raise = IntelligentRouter(tier_registry=registry, metadata_resolver=_RaisingResolver())
    assert router_raise.metadata_for(_PREFERRED_ID) is None


# ----- M1 follow-up: fail-open paths must WARN, never stay DEBUG-silent -----------------
#
# This repo has been burned repeatedly by silent fail-soft (R9-016/R9-019): "the user chose
# a model but the tier served instead" must be visible at the DEFAULT log level (INFO), not
# only when an operator happens to be running at DEBUG. Both tests below reconfigure the
# shared loguru sink against pytest's ``capsys``-substituted stderr (the same capture idiom
# ``packages/core/tests/unit/test_logging.py`` uses: ``reset_for_testing()`` to clear any
# stale sink bound to a PRE-capsys stderr, then ``get_logger(..., config=...)`` to attach a
# fresh sink the current test's ``capsys`` can see) and restore a clean slate afterwards so
# no state leaks into later tests.


def test_capability_gate_skip_logs_warning_not_debug(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The capability-gate skip (tools-incapable preferred model on a tools-requiring turn)
    # must WARN. Calling the gate twice proves the once-per-loop-instance dedup
    # (``_preferred_gate_logged``) still holds at the new level — exactly one line, not two.
    registry, _subs = _frontier_registry_with_subs()
    passthrough = ScriptedBackend(
        [ScriptedRound(text="preferred says hi")],
        provider_name="openrouter",
        model_name=_PREFERRED_ID,
    )
    resolver = _MapResolver({_PREFERRED_ID: _md(tools_supported=False)})
    loop, _writer = _build_loop(
        persona=_persona(
            preferred_model=_PREFERRED_ID, intelligent=IntelligentRoutingConfig(enabled=True)
        ),
        registry=registry,
        preferred_backend_provider=_provider_returning(passthrough),
        intelligent_router=IntelligentRouter(tier_registry=registry, metadata_resolver=resolver),
    )
    tools_ctx = loop._build_routing_context(
        "hello", _conversation(), turn_has_image=False
    ).model_copy(update={"requires_strong_tools": True})

    plog.reset_for_testing()
    plog.get_logger("runtime.loop", config=PersonaCoreConfig(log_format="pretty"))
    try:
        assert loop._preferred_model_for_turn(tools_ctx) is None
        assert loop._preferred_model_for_turn(tools_ctx) is None  # second call: no re-log
    finally:
        plog.reset_for_testing()
    out = capsys.readouterr().err

    assert "WARNING" in out
    assert _PREFERRED_ID in out
    warn_lines = [ln for ln in out.splitlines() if _PREFERRED_ID in ln]
    assert len(warn_lines) == 1, f"expected exactly one warning line, got {warn_lines!r}"


def test_provider_none_logs_warning_with_actionable_cause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A ``preferred_backend_provider`` returning None (no PERSONA_OPENROUTER_API_KEY
    # in-process, or passthrough construction failed) must WARN with the actionable cause
    # named in the message — an operator should not have to guess why the tier served
    # instead of the persona's chosen model. Calling twice proves the once-per-loop-instance
    # dedup (``_preferred_provider_none_logged``) still holds at the new level.
    registry, subs = _frontier_registry_with_subs()
    loop, _writer = _build_loop(
        persona=_persona(preferred_model=_PREFERRED_ID),
        registry=registry,
        preferred_backend_provider=_provider_returning(None),  # always None: no passthrough
    )

    plog.reset_for_testing()
    plog.get_logger("runtime.loop", config=PersonaCoreConfig(log_format="pretty"))
    try:
        fronted_1 = loop._front_preferred_backend(subs[0], _PREFERRED_ID)
        fronted_2 = loop._front_preferred_backend(subs[0], _PREFERRED_ID)  # no re-log
    finally:
        plog.reset_for_testing()
    out = capsys.readouterr().err

    assert fronted_1 is subs[0]  # fail-open: tier backend unchanged
    assert fronted_2 is subs[0]
    assert "WARNING" in out
    assert _PREFERRED_ID in out
    assert "PERSONA_OPENROUTER_API_KEY" in out
    warn_lines = [ln for ln in out.splitlines() if _PREFERRED_ID in ln]
    assert len(warn_lines) == 1, f"expected exactly one warning line, got {warn_lines!r}"
