"""Spec V2 T12 external smoke — 4 explicit measurement gates per Phase-4 LOCK.

🟦 **Operator-pass disposition.** This file is marked
``@pytest.mark.external`` and skipped without ``PERSONA_STT_API_KEY``
(Spec 02 D-02-11 + V1 T08 LiveKit-Server env-gated convention). Default
``pytest`` does NOT run it; operators invoke explicitly via
``pytest -m external``.

**Four measurement gates per decisions.md §"T12 measurement gates"** —
T05 unit tests record baselines but DO NOT assert thresholds; this file
is the binding operator-pass gate:

1. **Deepgram TTFT-to-first-FINAL** (NOT first-INTERIM) ≤250 ms P50 /
   ≤400 ms P95. Falsification route: Speechmatics swap behind same
   Protocol seam (D-V2-1 alternative provider; ``persona-voice``
   adapter-boundary discipline absorbs the swap without callers-side
   changes).
2. **Deepgram Arabic WER ≤25 %** on dialect mix (MSA + Egypt + Gulf +
   Levant operator-curated short corpus). Falsification route: per-
   session route AR → Speechmatics via
   ``PERSONA_STT_PROVIDER=speechmatics`` + ``PERSONA_STT_LANGUAGE_HINT=ar``.
3. **Silero wall-clock onset latency INCLUDING `SileroFramer`** on actual
   deployment CPU ≤150 ms P95. Falsification route: V4 semantic
   turn-detector overlay tightening; >150 ms forces v0.2 VAD-library
   re-evaluation (Pyannote VAD / ten-vad).
4. **Silero FP rate on speech_started ≤30 %** (V4 cancellation within
   200 ms as production telemetry proxy). Falsification route: V4
   semantic overlay tightening; raise ``activation_threshold`` to 0.8
   + re-measure quiet-speech recall.

**Gate semantics.** Each test asserts the threshold + emits a structured
falsification message on miss. The operator reads the message + selects
the documented falsification route per decisions.md. Misses ARE NOT
crashes — they are signal for the operator-pass review (CSA-3
disposition: 🟦 operator-passed at T13 close-out).

Why pytest.fail vs pytest.xfail: gates are MEASUREMENTS with thresholds,
not "expected failures." A test that XFAILs in production telemetry on
day one would be ignored; a test that fails surfaces the falsification
trigger. Operators run T12 deliberately during deploy + before T13
sign-off.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(
        os.environ.get("PERSONA_STT_API_KEY") is None,
        reason="real Deepgram API key required; set PERSONA_STT_API_KEY",
    ),
]


# ---------- shared scaffolding ----------------------------------------------


def _voice_tests_dir() -> Path:
    return Path(__file__).resolve().parent.parent


_TESTS_DIR_STR = str(_voice_tests_dir())
if _TESTS_DIR_STR not in sys.path:
    sys.path.insert(0, _TESTS_DIR_STR)


def _make_stt_config(model: str = "nova-3", language: str = "en") -> object:
    """Build a real StreamingSTTConfig from environment.

    Imported lazily so collection succeeds even when the api_key is unset
    (then pytestmark skips the whole module).
    """
    from persona_voice.stt.config import StreamingSTTConfig

    return StreamingSTTConfig(
        provider="deepgram",
        model=model,
        api_key=os.environ["PERSONA_STT_API_KEY"],  # type: ignore[arg-type]
        language_hint=language,
        vad_library="silero",
    )


# ---------- Gate #1 — TTFT-to-first-FINAL P50/P95 ---------------------------


@pytest.mark.asyncio
async def test_gate_1_deepgram_ttft_to_first_final_p50_p95_within_budget() -> None:
    """Phase-4 LOCK gate #1: Deepgram TTFT-to-first-FINAL ≤250 ms P50 / ≤400 ms P95.

    Operator-pass disposition: pushes a corpus of synthetic short
    utterances; records wall-clock from last push_audio() to first
    Transcript(is_final=True). Below thresholds → green; above → fail
    surfacing the documented Speechmatics-swap falsification route.

    NOTE: This is a structural test scaffold. The synthetic-utterance
    corpus assumes operator-curated audio at
    ``packages/voice/tests/external/_corpus/short_en/``. Without the
    corpus the test SKIPS with a clear message — the operator populates
    the corpus + re-runs.
    """
    corpus_dir = _voice_tests_dir() / "external" / "_corpus" / "short_en"
    if not corpus_dir.exists():
        pytest.skip(
            "operator-pass corpus missing — populate tests/external/_corpus/short_en/ "
            "with short English utterance audio (PCM16 16 kHz mono .raw or .wav) "
            "before re-running gate #1"
        )

    from persona_voice.stt.deepgram_backend import DeepgramStreamingSTT

    latencies_ms: list[float] = []
    for audio_path in sorted(corpus_dir.glob("*.raw")):
        backend = DeepgramStreamingSTT(_make_stt_config())  # type: ignore[arg-type]
        pcm = audio_path.read_bytes()
        # Push the full utterance, then time-to-first-FINAL.
        await backend.push_audio(pcm, 16_000)
        t_start = time.perf_counter()
        async for transcript in backend.transcripts():
            if transcript.is_final:
                latencies_ms.append((time.perf_counter() - t_start) * 1000.0)
                break
        await backend.close()

    if not latencies_ms:
        pytest.skip("no utterances yielded a FINAL transcript; check corpus + key")

    sorted_l = sorted(latencies_ms)
    p50 = sorted_l[len(sorted_l) // 2]
    p95_idx = int(len(sorted_l) * 0.95)
    p95 = sorted_l[min(p95_idx, len(sorted_l) - 1)]

    falsification = (
        f"GATE #1 MISS: TTFT-to-first-FINAL P50={p50:.1f} ms / P95={p95:.1f} ms "
        f"vs 250/400 ms budget. Falsification route per decisions.md: route to "
        f"Speechmatics behind same Protocol seam (PERSONA_STT_PROVIDER=speechmatics)."
    )
    assert p50 <= 250.0, falsification
    assert p95 <= 400.0, falsification


# ---------- Gate #2 — Arabic WER ≤25 % on dialect mix -----------------------


@pytest.mark.asyncio
async def test_gate_2_deepgram_arabic_wer_on_dialect_mix_within_budget() -> None:
    """Phase-4 LOCK gate #2: Deepgram Arabic WER ≤25 % on MSA + EG + Gulf + Levant.

    Operator-pass disposition: corpus at
    ``packages/voice/tests/external/_corpus/arabic_dialects/<dialect>/`` with
    matching ``.txt`` ground-truth transcripts per audio file. Computes
    aggregate WER; below threshold → green; above → fail surfacing the
    per-session AR-route-to-Speechmatics falsification.
    """
    corpus_dir = _voice_tests_dir() / "external" / "_corpus" / "arabic_dialects"
    if not corpus_dir.exists():
        pytest.skip(
            "operator-pass corpus missing — populate tests/external/_corpus/"
            "arabic_dialects/{msa,eg,gulf,levant}/ with PCM16 16 kHz audio + "
            ".txt ground-truth per file before re-running gate #2"
        )

    from persona_voice.stt.deepgram_backend import DeepgramStreamingSTT

    total_errors = 0
    total_words = 0
    for dialect_dir in sorted(corpus_dir.iterdir()):
        if not dialect_dir.is_dir():
            continue
        for audio_path in sorted(dialect_dir.glob("*.raw")):
            transcript_path = audio_path.with_suffix(".txt")
            if not transcript_path.exists():
                continue
            ground_truth = transcript_path.read_text().strip().split()
            total_words += len(ground_truth)

            backend = DeepgramStreamingSTT(_make_stt_config(language="ar"))  # type: ignore[arg-type]
            pcm = audio_path.read_bytes()
            await backend.push_audio(pcm, 16_000)
            hypothesis: list[str] = []
            async for transcript in backend.transcripts():
                if transcript.is_final:
                    hypothesis.extend(transcript.text.split())
            await backend.close()
            # Levenshtein-style word edit distance — minimal implementation
            # (operator-pass corpus is small; we don't need a full WER lib).
            total_errors += _word_edit_distance(ground_truth, hypothesis)

    if total_words == 0:
        pytest.skip("no Arabic utterances scored; check corpus + ground-truth files")

    wer = total_errors / total_words
    falsification = (
        f"GATE #2 MISS: Arabic WER={wer:.1%} vs 25 % budget. Falsification "
        f"route per decisions.md: route AR → Speechmatics via "
        f"PERSONA_STT_PROVIDER=speechmatics + PERSONA_STT_LANGUAGE_HINT=ar."
    )
    assert wer <= 0.25, falsification


def _word_edit_distance(ref: list[str], hyp: list[str]) -> int:
    """Minimal word-level edit distance for WER computation."""
    m, n = len(ref), len(hyp)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if ref[i - 1] == hyp[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    return dp[m][n]


# ---------- Gate #3 — Silero wall-clock onset ≤150 ms P95 incl. SileroFramer


@pytest.mark.asyncio
async def test_gate_3_silero_wall_clock_onset_p95_with_reframer() -> None:
    """Phase-4 LOCK gate #3: Silero wall-clock onset INCLUDING SileroFramer
    on actual deployment CPU ≤150 ms P95.

    Uses :func:`benchmark_onset_latency` (T05); runs 200 silence-to-speech
    onsets of rendered speech (Cartesia TTS, the committed V8 corpus slice)
    at the production threshold, the model reset before each; asserts P95
    ≤150 ms. Below threshold → green; above → fail surfacing the
    V4-overlay-tightening falsification route. Records benchmark output for
    the operator-pass audit trail. Too few captured onsets is a failure, not
    a skip: a gate that cannot measure has not passed.
    """
    from _speech_audio import speech_pcm  # type: ignore[import-not-found]
    from persona_voice.stt.config import StreamingSTTConfig
    from persona_voice.stt.vad_silero import (
        SileroVADAdapter,
        benchmark_onset_latency,
    )

    config = StreamingSTTConfig(
        provider="deepgram",
        api_key="placeholder-for-vad-only-test",  # type: ignore[arg-type]
        vad_library="silero",
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=32,
    )
    adapter = SileroVADAdapter(config)
    await adapter.load()
    metrics = await benchmark_onset_latency(
        adapter,
        speech=speech_pcm(200),
        iterations=200,
    )
    await adapter.close()

    assert metrics["iterations"] >= 50, (
        f"GATE #3 UNMEASURED: the benchmark captured only {metrics['iterations']:.0f}/200 "
        f"onsets, too few for a P95; the speech burst no longer crosses the activation "
        f"threshold (check the silero-vad-lite version and the corpus slice)"
    )

    p50 = metrics["p50_ms"]
    p95 = metrics["p95_ms"]
    falsification = (
        f"GATE #3 MISS: Silero wall-clock onset P95={p95:.1f} ms vs 150 ms "
        f"budget (P50={p50:.1f} ms; D-V2-2 wall-clock framing acknowledges "
        f"116-121 ms worst-case). Falsification route per decisions.md: "
        f"V4 semantic turn-detector overlay tightening; if >150 ms persistent, "
        f"v0.2 VAD-library re-evaluation (Pyannote VAD / ten-vad)."
    )
    assert p95 <= 150.0, falsification


# ---------- Gate #4 — Silero FP rate on speech_started ≤30 % ----------------


@pytest.mark.asyncio
async def test_gate_4_silero_fp_rate_on_bleedthrough_corpus_within_budget() -> None:
    """Phase-4 LOCK gate #4: Silero FP rate on speech_started ≤30 %.

    Operator-pass disposition: corpus of TTS audio (persona-side bleed-
    through simulation) OR silence audio with background noise at
    ``packages/voice/tests/external/_corpus/bleedthrough/``. Counts
    speech_started emissions vs ground-truth (zero speech expected); FP
    rate = emissions / utterance_count. Below threshold → green; above →
    fail surfacing the activation_threshold→0.8 falsification route.
    """
    corpus_dir = _voice_tests_dir() / "external" / "_corpus" / "bleedthrough"
    if not corpus_dir.exists():
        pytest.skip(
            "operator-pass corpus missing — populate tests/external/_corpus/"
            "bleedthrough/ with PCM16 16 kHz audio of TTS playback / background "
            "noise (zero true speech) before re-running gate #4"
        )

    from persona_voice.stt.config import StreamingSTTConfig
    from persona_voice.stt.vad_silero import SileroVADAdapter

    config = StreamingSTTConfig(
        provider="deepgram",
        api_key="placeholder-for-vad-only-test",  # type: ignore[arg-type]
        vad_library="silero",
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=50,
    )

    total_utterances = 0
    fp_emissions = 0
    for audio_path in sorted(corpus_dir.glob("*.raw")):
        total_utterances += 1
        adapter = SileroVADAdapter(config)
        await adapter.load()
        await adapter.push_audio(audio_path.read_bytes(), 16_000)
        # Drain the queue with a short timeout — any emission within budget
        # counts as a false-positive (corpus has zero true speech).
        try:
            while True:
                event = await asyncio.wait_for(
                    adapter._event_queue.get(),  # noqa: SLF001
                    timeout=0.1,
                )
                if event is not None and event.event_type == "speech_started":
                    fp_emissions += 1
                    break
        except TimeoutError:
            pass
        await adapter.close()

    if total_utterances == 0:
        pytest.skip("bleedthrough corpus is empty")

    fp_rate = fp_emissions / total_utterances
    falsification = (
        f"GATE #4 MISS: Silero FP rate on bleedthrough={fp_rate:.1%} "
        f"({fp_emissions}/{total_utterances}) vs 30 % budget. Falsification "
        f"route per decisions.md: V4 semantic overlay tightening; raise "
        f"PERSONA_STT_SILERO_ACTIVATION_THRESHOLD to 0.8 + re-measure "
        f"quiet-speech recall."
    )
    assert fp_rate <= 0.30, falsification
