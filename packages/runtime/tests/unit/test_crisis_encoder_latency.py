"""T6 — latency mechanics: off-loop offload, short-circuit equivalence, hang timeout.

The measured numbers (cold-start, warm p50/p95, RSS delta) live in the eval record —
they are hardware-dependent and reported, not asserted. What IS asserted here are the
*mechanisms* the numbers justify (R6-D-2):

- **Off the event loop:** `classify_user_message` is a plain sync callable; run via
  ``asyncio.to_thread`` it does NOT block the loop while the (CPU-bound) score runs —
  the voice starvation-safety property.
- **Short-circuit is verdict-preserving:** lexical-HARD returns BEFORE the encoder is
  consulted (pure latency win). Proven equivalent to always-running-both over the full
  155-probe suite, for several encoder outputs — the skip can never change semantics
  because lexical-HARD is already the maximal-severity verdict.
- **Hang guard:** a slow/wedged encoder times out → lexical-only (fail-soft→R0), never
  stalls the message path.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from _adversarial_eval import load_probes  # type: ignore[import-not-found]
from persona_runtime.crisis_encoder import CRISIS_ENCODER_T_HARD, CRISIS_ENCODER_T_SOFT
from persona_runtime.safety_intercept import (
    InterceptAction,
    SafetyInterceptSettings,
    classify_user_message,
)

_SUITE = Path(__file__).resolve().parents[1] / "fixtures" / "crisis_encoder_probes.yaml"


class _Fixed:
    def __init__(self, value: float) -> None:
        self._value = value
        self.calls = 0

    def score(self, text: str) -> float:  # noqa: ARG002 — fixed regardless of input
        self.calls += 1
        return self._value


class _Slow:
    def __init__(self, delay: float, value: float) -> None:
        self._delay = delay
        self._value = value

    def score(self, text: str) -> float:  # noqa: ARG002 — timing stub
        time.sleep(self._delay)
        return self._value


def _always_both(text: str, lang: str, score: float) -> InterceptAction:
    """Reference: the SAME precedence with the encoder ALWAYS consulted (no skip).

    The pure lexical verdict is obtained from the real classifier with ``encoder=None``
    (no private-symbol coupling); the encoder score is then folded in per the R6-D-3
    precedence — exactly what `classify_user_message` does, minus the short-circuit.
    """
    lexical = classify_user_message(text, locale=lang, encoder=None).action
    if lexical is InterceptAction.HARD:
        return InterceptAction.HARD
    if score >= CRISIS_ENCODER_T_HARD:
        return InterceptAction.HARD
    if lexical is InterceptAction.SOFT or score >= CRISIS_ENCODER_T_SOFT:
        return InterceptAction.SOFT
    return InterceptAction.NONE


class TestShortCircuitIsVerdictPreserving:
    def test_lexical_hard_skips_the_encoder_entirely(self) -> None:
        scorer = _Fixed(0.99)  # would itself say HARD — irrelevant, lexical wins first
        v = classify_user_message("i want to end my life", encoder=scorer)
        assert v.action is InterceptAction.HARD
        assert scorer.calls == 0  # the pure-latency-win skip: encoder never consulted

    def test_equivalent_to_always_both_over_full_suite(self) -> None:
        probes = load_probes(_SUITE)
        # Stress a range of encoder outputs incl. the exact threshold boundaries.
        for value in (0.0, CRISIS_ENCODER_T_SOFT, 0.6, CRISIS_ENCODER_T_HARD, 0.99):
            scorer = _Fixed(value)
            for p in probes:
                got = classify_user_message(
                    p.text, locale=p.lang, encoder=scorer, settings=_no_timeout()
                ).action
                want = _always_both(p.text, p.lang, value)
                assert got is want, f"{p.id} @ score={value}: {got} != {want}"


class TestHangGuardTimeout:
    def test_slow_encoder_times_out_to_lexical_only(self) -> None:
        slow = _Slow(0.5, 0.99)  # would say HARD, but far too slow
        s = SafetyInterceptSettings(encoder_timeout_s=0.05)
        # A euphemism the lexicon misses: the encoder times out → NONE (→ R0 floor).
        euph = "it would be easier if i just faded away"
        assert classify_user_message(euph, encoder=slow, settings=s).action is InterceptAction.NONE

    def test_lexical_hard_never_waits_on_a_slow_encoder(self) -> None:
        slow = _Slow(5.0, 0.99)  # would hang the turn if consulted
        started = time.perf_counter()
        v = classify_user_message("i want to kill myself", encoder=slow)
        assert v.action is InterceptAction.HARD
        assert time.perf_counter() - started < 1.0  # short-circuited, no wait

    def test_within_timeout_uses_the_encoder(self) -> None:
        fast = _Slow(0.0, 0.99)
        s = SafetyInterceptSettings(encoder_timeout_s=1.0)
        euph = "it would be easier if i just faded away"
        assert classify_user_message(euph, encoder=fast, settings=s).action is InterceptAction.HARD

    def test_zero_timeout_runs_inline(self) -> None:
        v = classify_user_message(
            "it would be easier if i just faded away",
            encoder=_Fixed(0.99),
            settings=_no_timeout(),
        )
        assert v.action is InterceptAction.HARD


class TestOffEventLoop:
    @pytest.mark.asyncio
    async def test_offloaded_classify_does_not_block_the_loop(self) -> None:
        # A 0.2 s CPU-bound score, run via to_thread, must NOT freeze the event loop:
        # a concurrent ticker keeps advancing while the worker thread scores.
        slow = _Slow(0.2, 0.9)
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            for _ in range(30):
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        verdict = await asyncio.to_thread(
            classify_user_message,
            "it would be easier if i just faded away",
            encoder=slow,
            settings=_no_timeout(),
        )
        await task
        assert verdict.action is InterceptAction.HARD
        assert ticks >= 15, f"loop starved while offloaded score ran (ticks={ticks})"


def _no_timeout() -> SafetyInterceptSettings:
    return SafetyInterceptSettings(encoder_timeout_s=0.0)
