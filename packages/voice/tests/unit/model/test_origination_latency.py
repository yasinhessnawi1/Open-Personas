"""T9 — the latency / placement proof for the origination gate (Spec A9, criterion 2).

The structural half was proven at T3 (a no-cue / gate-ordinary turn makes NO model dispatch and runs
generation unchanged). This is the MEASURED half: on a no-intent turn — the overwhelming case — the
gate adds *exactly one thing* over today's turn, the cheap ``detect_standing_cue`` regex (the judge
is gated behind it and never fires without a cue, asserted here too). So the regex's cost IS the
gate's whole before/after delta on a no-cue turn, and it must be negligible against the voice
first-audio budget (800 ms P50 / 1.5 s P95, R-V1-3) — nothing model-judged runs on the event loop.

The measured p50/p95 are printed so the number is evidence, not an assertion in the dark.
"""

from __future__ import annotations

import time

import pytest
from persona_runtime.task_origination import (
    StandingIntentRecognizer,
    StandingJudgment,
    StandingVerdict,
)
from persona_runtime.task_origination.cues import detect_standing_cue

# A corpus of realistic NO-CUE voice utterances (ordinary conversation — no standing/recurring
# marker), the overwhelming case a live call sees. None of these must trip the cue net.
_NO_CUE_CORPUS = [
    "what's the weather like today",
    "can you tell me a joke",
    "how are you doing",
    "thanks, that's helpful",
    "what did we talk about yesterday",
    "tell me about the news right now",
    "who won the game last night",
    "i'm feeling a bit tired",
    "what's your favourite colour",
    "read me that article",
    "hmm, let me think about it",
    "actually never mind",
    "that sounds good to me",
    "can you help me with something",
    "what time is it in Oslo",
]

#: Generous vs the 800 ms voice first-audio budget (~160x margin) — machine-independent, and the
#: point is "negligible", not a tight micro-bound. The regex over a short utterance is microseconds.
_MAX_P95_MS = 5.0


def _percentile(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def test_no_cue_utterances_trip_no_cue_and_stay_cheap() -> None:
    # Correctness: not one no-cue utterance admits the turn to the (expensive) judge.
    for utterance in _NO_CUE_CORPUS:
        assert detect_standing_cue(utterance) is None, utterance

    # Measurement: the gate's whole added cost on a no-cue turn is this regex.
    samples_ms: list[float] = []
    for _ in range(200):
        for utterance in _NO_CUE_CORPUS:
            start = time.perf_counter()
            detect_standing_cue(utterance)
            samples_ms.append((time.perf_counter() - start) * 1000.0)

    p50 = _percentile(samples_ms, 50.0)
    p95 = _percentile(samples_ms, 95.0)
    print(
        f"\n[A9 criterion-2] no-cue gate overhead: p50={p50:.4f}ms p95={p95:.4f}ms "
        f"(budget 800ms P50 / 1500ms P95)"
    )
    assert p95 < _MAX_P95_MS  # negligible vs the voice budget — the no-intent turn is unchanged


@pytest.mark.asyncio
async def test_no_cue_turn_never_calls_the_model_judge() -> None:
    # The structural placement guarantee (criterion 2): behind the cue gate, the model judge is
    # NEVER consulted on a no-cue turn — so nothing model-judged runs on the event loop for it.
    class _CountingJudge:
        def __init__(self) -> None:
            self.calls = 0

        async def judge(self, message: str, *, language: str) -> StandingJudgment:  # noqa: ARG002
            self.calls += 1
            return StandingJudgment(verdict=StandingVerdict.NOW_WORK)

    judge = _CountingJudge()
    recognizer = StandingIntentRecognizer(judge)  # type: ignore[arg-type]
    for utterance in _NO_CUE_CORPUS:
        outcome = await recognizer.recognize(utterance, language="en")
        assert outcome.kind.value == "ordinary"
    assert judge.calls == 0  # zero model dispatch on the no-intent path
