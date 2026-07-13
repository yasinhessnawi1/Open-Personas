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

import asyncio
import threading
import time
from typing import TYPE_CHECKING

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from loguru import logger as _loguru_logger
from persona.backends import BackendConfig
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime import crisis_encoder as crisis_encoder_mod
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.agentic.run import RunStatus
from persona_runtime.crisis_encoder import (
    DEFAULT_WARMUP_DEADLINE_S,
    WARMUP_THREAD_NAME,
    CrisisEncoder,
    CrisisEncoderError,
    build_crisis_encoder,
    start_crisis_encoder_warmup,
    warmup_deadline_from_env,
)
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.safety_intercept import (
    InterceptAction,
    SafetyInterceptSettings,
    classify_user_message,
)
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

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


# ------------------------------------------------ R9-027: bounded, never-joined warm-up


@pytest.fixture
def loguru_capture() -> Iterator[list[str]]:
    """Loguru sink capturing every ≥WARNING message (persona.logging wraps loguru,
    so stdlib ``caplog`` sees nothing — the test_m2_multiround_usage.py pattern)."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


class _BlockingEmbedder:
    """A body whose ``encode`` blocks on an Event — a stuck HF download stand-in.

    ``entered`` flips when the load thread is genuinely inside the encode (so a
    test can assert thread state mid-load); ``release`` lets the test end the
    "download" — after release, encode returns separable-enough vectors for the
    head fit to complete (the late-arrival recovery leg). The 30 s wait ceiling
    is test-safety only (a leaked thread unblocks itself eventually).
    """

    model_name = "blocking-stub"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()

    @property
    def dimension(self) -> int:
        return 3

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        self.entered.set()
        if not self.release.wait(timeout=30.0):  # pragma: no cover - test-safety ceiling
            msg = "test blocking embedder never released"
            raise RuntimeError(msg)
        out: list[list[float]] = []
        for t in texts:
            hit = 1.0 if any(tok in t for tok in ("die", "dø", "dö", "end my", "kill")) else 0.0
            out.append([hit, min(len(t) / 80.0, 1.0), 1.0])
        return out


class TestWarmupBounds:
    """R9-027: the warm-up is deadline-bounded, daemon-threaded, and never joined."""

    def test_deadline_env_matrix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSONA_CRISIS_WARMUP_DEADLINE_S", raising=False)
        assert warmup_deadline_from_env() == DEFAULT_WARMUP_DEADLINE_S
        monkeypatch.setenv("PERSONA_CRISIS_WARMUP_DEADLINE_S", "30")
        assert warmup_deadline_from_env() == 30.0
        monkeypatch.setenv("PERSONA_CRISIS_WARMUP_DEADLINE_S", "0")
        assert warmup_deadline_from_env() == 0.0  # <= 0 ⇒ no deadline (operator opt-out)
        monkeypatch.setenv("PERSONA_CRISIS_WARMUP_DEADLINE_S", "not-a-number")
        assert warmup_deadline_from_env() == DEFAULT_WARMUP_DEADLINE_S  # typo ⇒ default

    @pytest.mark.asyncio
    async def test_deadline_degrades_unfitted_score_fails_soft_then_late_recovery(
        self, monkeypatch: pytest.MonkeyPatch, loguru_capture: list[str]
    ) -> None:
        # Call-time _fit acquire must fail FAST in this test (the production 5 s
        # bound exists for real pool workers; here it would just slow the test).
        monkeypatch.setattr(crisis_encoder_mod, "_FIT_LOCK_TIMEOUT_S", 0.05)
        body = _BlockingEmbedder()
        enc = CrisisEncoder(embedder=body)

        started = time.monotonic()
        task = start_crisis_encoder_warmup(enc, deadline_s=0.2)
        await task  # must RETURN at the deadline — never wait for the stuck load
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"warm-up task did not detach at the deadline ({elapsed:.1f}s)"
        assert body.entered.wait(timeout=5.0), "the load thread never started loading"

        # Degraded posture: unfitted; a call-time score raises CrisisEncoderError
        # (the single fail-soft seam) and classify absorbs it to lexical-only.
        assert enc._head is None  # noqa: SLF001 — deadline hit ⇒ still unfitted
        with pytest.raises(CrisisEncoderError):
            enc.score(_EUPH)
        verdict = classify_user_message(
            _EUPH,
            settings=SafetyInterceptSettings(encoder_timeout_s=0),  # inline call, no pool
            encoder=enc,
        )
        assert verdict.action is InterceptAction.NONE  # euphemism: lexicon miss, encoder down
        assert any("deadline" in line for line in loguru_capture), loguru_capture

        # Late-arrival recovery: release the "download"; the daemon thread finishes
        # the fit; encoder verdicts resume without any re-warm call.
        body.release.set()
        deadline = time.monotonic() + 10.0
        while enc._head is None and time.monotonic() < deadline:  # noqa: SLF001
            await asyncio.sleep(0.02)
        assert enc._head is not None, "the background load never completed after release"  # noqa: SLF001
        assert 0.0 <= enc.score(_EUPH) <= 1.0

    @pytest.mark.asyncio
    async def test_load_thread_is_daemon_and_not_the_default_executor(self) -> None:
        # THE shutdown-bound property, asserted structurally: the load thread is a
        # DAEMON with our dedicated name — not an ``asyncio_N`` default-executor
        # worker (joined unbounded by loop.shutdown_default_executor at TestClient
        # exit — the 2026-07-11 CI hang) and not a ``ThreadPoolExecutor`` worker
        # (joined by concurrent.futures' atexit hook on real SIGTERM).
        body = _BlockingEmbedder()
        enc = CrisisEncoder(embedder=body)
        task = start_crisis_encoder_warmup(enc, deadline_s=0.1)
        try:
            # Yield so the task body runs (it spawns the loader thread before its
            # first await); wait for the load OFF the loop thread — a bare
            # ``entered.wait`` here would block the loop and the task would never run.
            await asyncio.sleep(0)
            assert await asyncio.to_thread(body.entered.wait, 5.0)
            loaders = [
                t for t in threading.enumerate() if t.name == WARMUP_THREAD_NAME and t.is_alive()
            ]
            assert loaders, "no dedicated warm-up thread found"
            assert all(t.daemon for t in loaders), "warm-up thread must be a daemon"
            assert not any(t.name.startswith("asyncio_") for t in loaders), (
                "load must never ride the asyncio default executor"
            )
        finally:
            body.release.set()
            await task

    @pytest.mark.asyncio
    async def test_cancel_detaches_immediately_from_a_stuck_load(self) -> None:
        # The api lifespan cancels the warm task at shutdown. Even with NO deadline
        # (operator opt-out) and a genuinely stuck load, cancel must end the awaiting
        # side promptly — the thread is abandoned, never joined.
        body = _BlockingEmbedder()
        enc = CrisisEncoder(embedder=body)
        task = start_crisis_encoder_warmup(enc, deadline_s=0)  # unbounded wait arm
        await asyncio.sleep(0)  # let the task spawn the loader thread
        assert await asyncio.to_thread(body.entered.wait, 5.0)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < 2.0, "cancel must not wait on the stuck load"
        body.release.set()  # unblock the leaked daemon thread so it exits cleanly
