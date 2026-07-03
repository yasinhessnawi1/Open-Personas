"""T8 — three-path integration + fail-soft→R0 (Spec R6, R6-D-3/5).

Proves the encoder is WIRED into the real chat + agentic loops (not a hand-assembled
harness) and that every path routes through the SAME composed
``classify_user_message``:

- **Encoder-HARD bypasses generation** — a euphemism the lexicon misses, scored ≥ T_hard,
  makes the real ``ConversationLoop.turn`` / ``AgenticLoop.run`` emit the SafeCompletion
  and NEVER call the model (the encoder is genuinely consulted + acted on).
- **Fail-soft→R0** — a BROKEN encoder never loses the lexical-HARD floor (an explicit
  message still bypasses), and a euphemism it can't score degrades to normal generation
  (lexical-only), never a crash, never silence.
- **``None`` ⇒ V11** — the default (no encoder) is byte-identical to V11 (the whole
  existing loop suite runs with ``crisis_encoder=None`` and is the regression proof).

The voice path's wiring + off-loop offload is proven in ``test_reply_producer`` (T8); the
composition-root injection in ``test_api_runtime_factory`` (api) + ``test_launcher`` (voice).
"""

from __future__ import annotations

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.agentic.run import RunStatus
from persona_runtime.crisis_encoder import (
    CrisisEncoder,
    CrisisEncoderError,
    build_crisis_encoder,
    start_crisis_encoder_warmup,
)
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
_EUPH = "i just want to fade out of the picture and not come back"  # lexical-miss euphemism
_EXPLICIT = "i want to kill myself tonight"


class _Fixed:
    """A CrisisScorer returning a fixed score; records that it was consulted."""

    def __init__(self, value: float) -> None:
        self._value = value
        self.calls = 0

    def score(self, text: str) -> float:  # noqa: ARG002 — fixed regardless of input
        self.calls += 1
        return self._value


class _Raises:
    def score(self, text: str) -> float:  # noqa: ARG002 — fault stub
        msg = "simulated encoder fault"
        raise CrisisEncoderError(msg)


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="b", constraints=[]),
    )


def _stores() -> dict[str, FakeStore]:
    return {k: FakeStore() for k in ("identity", "self_facts", "worldview", "episodic")}


def _registry(backend: ScriptedBackend) -> TierRegistry:
    reg = TierRegistry(
        {n: TierConfig(name=n, backend_config=_CFG) for n in ("frontier", "mid", "small")}
    )
    reg._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]  # noqa: SLF001
    return reg


def _chat_loop(backend: ScriptedBackend, encoder: object | None) -> ConversationLoop:
    return ConversationLoop(
        persona=_persona(),
        stores=_stores(),  # type: ignore[arg-type]
        toolbox=Toolbox([], allow_list=None),
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=_registry(backend),
        turn_log_writer=MemoryTurnLogWriter(),
        crisis_encoder=encoder,  # type: ignore[arg-type]
    )


def _agentic_loop(backend: ScriptedBackend, encoder: object | None) -> AgenticLoop:
    return AgenticLoop(
        persona=_persona(),
        stores=_stores(),  # type: ignore[arg-type]
        toolbox=Toolbox([], allow_list=None),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=_registry(backend),
        crisis_encoder=encoder,  # type: ignore[arg-type]
    )


def _conv() -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=[])


async def _turn_text(loop: ConversationLoop, msg: str) -> str:
    chunks = [c async for c in loop.turn(_conv(), msg)]
    return "".join(getattr(c, "delta", "") or "" for c in chunks)


@pytest.mark.asyncio
class TestChatPathWiring:
    async def test_encoder_hard_bypasses_generation_and_is_consulted(self) -> None:
        enc = _Fixed(0.99)
        loop = _chat_loop(ScriptedBackend([ScriptedRound(text="MODEL_OUTPUT")]), enc)
        text = await _turn_text(loop, _EUPH)
        assert enc.calls > 0, "the real loop never consulted the encoder"
        assert "MODEL_OUTPUT" not in text, "generation was NOT bypassed"
        assert "an AI" in text, "the SafeCompletion (AI-disclosure) was not emitted"

    async def test_broken_encoder_keeps_the_lexical_hard_floor(self) -> None:
        loop = _chat_loop(ScriptedBackend([ScriptedRound(text="MODEL_OUTPUT")]), _Raises())
        text = await _turn_text(loop, _EXPLICIT)
        assert "MODEL_OUTPUT" not in text  # lexical-HARD fired despite the broken encoder
        assert "an AI" in text

    async def test_broken_encoder_on_euphemism_degrades_to_generation(self) -> None:
        # Fail-soft: the euphemism the lexicon misses falls through to normal generation
        # (lexical-only), never a crash, never a bypass.
        loop = _chat_loop(ScriptedBackend([ScriptedRound(text="MODEL_OUTPUT")]), _Raises())
        text = await _turn_text(loop, _EUPH)
        assert "MODEL_OUTPUT" in text

    async def test_none_encoder_is_v11_no_bypass_on_euphemism(self) -> None:
        loop = _chat_loop(ScriptedBackend([ScriptedRound(text="MODEL_OUTPUT")]), None)
        text = await _turn_text(loop, _EUPH)
        assert "MODEL_OUTPUT" in text  # V11: no encoder ⇒ euphemism generates normally


@pytest.mark.asyncio
class TestAgenticPathWiring:
    async def test_encoder_hard_bypasses_the_run_with_no_steps(self) -> None:
        enc = _Fixed(0.99)
        run = await _agentic_loop(ScriptedBackend([]), enc).run(_EUPH)
        assert enc.calls > 0
        assert run.status is RunStatus.COMPLETED
        assert run.steps == []  # bypass: the agentic loop never ran
        assert "an AI" in (run.output or "")

    async def test_broken_encoder_keeps_lexical_hard(self) -> None:
        run = await _agentic_loop(ScriptedBackend([]), _Raises()).run(_EXPLICIT)
        assert run.steps == []
        assert "an AI" in (run.output or "")


class TestWarmupHelpers:
    def test_build_crisis_encoder_is_lazy(self) -> None:
        enc = build_crisis_encoder()
        assert isinstance(enc, CrisisEncoder)
        assert enc._head is None  # noqa: SLF001 — no model loaded at construction (lazy)

    @pytest.mark.asyncio
    async def test_start_warmup_runs_warmup_off_loop(self) -> None:
        class _Stub:
            def __init__(self) -> None:
                self.warmed = 0

            def warmup(self) -> None:
                self.warmed += 1

        stub = _Stub()
        await start_crisis_encoder_warmup(stub)  # type: ignore[arg-type]
        assert stub.warmed == 1

    @pytest.mark.asyncio
    async def test_warmup_never_raises_on_a_failing_encoder(self) -> None:
        class _BadStub:
            def warmup(self) -> None:
                msg = "cold load failed"
                raise CrisisEncoderError(msg)

        # Best-effort: a failed warm is logged, not raised (the boot must not crash).
        await start_crisis_encoder_warmup(_BadStub())  # type: ignore[arg-type]
