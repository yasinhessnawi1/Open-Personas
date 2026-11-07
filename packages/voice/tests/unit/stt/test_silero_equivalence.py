"""The onnxruntime Silero runner scores like silero-vad-lite 0.3.0 did, on every architecture.

The voice VAD moved off ``silero-vad-lite`` so it installs on Linux aarch64. The tuned
thresholds (``PERSONA_STT_SILERO_ACTIVATION_THRESHOLD``, default 0.5) must keep meaning the
same thing, so the new runner is held to two references:

1. ``fixtures/silero_golden/v8_corpus_scores.json``: what silero-vad-lite 0.3.0 scored on
   x86_64 for every 32 ms window of the five V8 corpus files (rendered speech with pauses,
   barge-in and soft endings), framed by the production ``SileroFramer``.
2. ``fixtures/silero_v5_1/context_v5_1.json``: upstream's independent NumPy reference of
   Silero's own v5.1 wrapper, at 16 kHz and 8 kHz.

The gate, for both: every score within 1e-4 of the reference, and the same speech or
not-speech decision at thresholds 0.3, 0.5 and 0.7 for every window. CI runs this file on
x86_64 and on aarch64 (``.github/workflows/voice-platforms.yml``).
"""

from __future__ import annotations

import hashlib
import json
import struct
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from persona_voice.stt.silero_onnx import SILERO_MODEL_SHA256, SileroVAD
from persona_voice.stt.vad_silero import SILERO_SAMPLE_RATE_HZ, SileroFramer

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
_GOLDEN = _FIXTURES / "silero_golden" / "v8_corpus_scores.json"
_CORPUS = _FIXTURES / "v8_corpus"
_UPSTREAM = _FIXTURES / "silero_v5_1"

TOLERANCE = 1e-4
THRESHOLDS = (0.3, 0.5, 0.7)


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def assert_equivalent(name: str, got: list[float], want: list[float]) -> None:
    """The gate: same length, every score within TOLERANCE, the same decision per threshold."""
    assert len(got) == len(want), f"{name}: {len(got)} windows scored, reference has {len(want)}"
    worst = max(abs(g - w) for g, w in zip(got, want, strict=True))
    assert worst <= TOLERANCE, f"{name}: largest difference {worst:.3g} exceeds {TOLERANCE}"
    for threshold in THRESHOLDS:
        # The adapter's own comparison: voiced = score >= threshold (vad_silero.py).
        flipped = [
            i
            for i, (g, w) in enumerate(zip(got, want, strict=True))
            if (g >= threshold) != (w >= threshold)
        ]
        assert not flipped, f"{name}: windows {flipped} change decision at threshold {threshold}"


def _score_stream(frames: list[np.ndarray], rate: int) -> list[float]:
    vad = SileroVAD(rate)
    return [vad.process(frame) for frame in frames]


# ---------- 1. silero-vad-lite 0.3.0 on the V8 corpus ------------------------------


GOLDEN = _load(_GOLDEN)


def test_the_golden_scores_were_made_with_the_model_this_package_ships() -> None:
    assert GOLDEN["model_sha256"] == SILERO_MODEL_SHA256
    assert GOLDEN["sample_rate_hz"] == SILERO_SAMPLE_RATE_HZ
    assert sorted(GOLDEN["files"]) == sorted(p.name for p in _CORPUS.glob("*.raw"))


@pytest.mark.parametrize("name", sorted(GOLDEN["files"]))
def test_the_v8_corpus_scores_like_silero_vad_lite(name: str) -> None:
    raw = (_CORPUS / name).read_bytes()
    entry = GOLDEN["files"][name]
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"], (
        f"{name} changed since the golden run"
    )
    frames = list(SileroFramer().push(raw))
    assert_equivalent(name, _score_stream(frames, SILERO_SAMPLE_RATE_HZ), entry["scores"])


# ---------- 2. upstream's reference of Silero's own v5.1 wrapper --------------------


UPSTREAM = _load(_UPSTREAM / "context_v5_1.json")


def _upstream_samples() -> np.ndarray:
    wav_path = _UPSTREAM / "sample.wav"
    assert hashlib.sha256(wav_path.read_bytes()).hexdigest() == UPSTREAM["audio_sha256"]
    with wave.open(str(wav_path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16_000, 1, 2)
        pcm = wav.readframes(wav.getnframes())
    return np.asarray([v / 32768.0 for (v,) in struct.iter_unpack("<h", pcm)], dtype=np.float32)


def test_the_upstream_reference_used_the_model_this_package_ships() -> None:
    assert UPSTREAM["model_sha256"] == SILERO_MODEL_SHA256


@pytest.mark.parametrize("rate", [16_000, 8_000])
def test_scores_match_silero_v5_1_reference(rate: int) -> None:
    signal = _upstream_samples()[:: 16_000 // rate]
    window = rate * 32 // 1000
    frames = [signal[s : s + window] for s in range(0, signal.size - window + 1, window)]
    assert_equivalent(
        f"sample.wav@{rate}", _score_stream(frames, rate), UPSTREAM["probabilities"][str(rate)]
    )


# ---------- the gate itself ----------------------------------------------------------


def test_the_gate_catches_a_drift_past_1e_4() -> None:
    with pytest.raises(AssertionError, match="largest difference"):
        assert_equivalent("drift", [0.1, 0.2002], [0.1, 0.2])


def test_the_gate_allows_a_drift_inside_1e_4() -> None:
    assert_equivalent("close", [0.1, 0.20005], [0.1, 0.2])


def test_the_gate_catches_a_missing_window() -> None:
    with pytest.raises(AssertionError, match="windows scored"):
        assert_equivalent("short", [0.1], [0.1, 0.2])


def test_the_gate_catches_a_flipped_decision_inside_the_tolerance() -> None:
    with pytest.raises(AssertionError, match="threshold 0.5"):
        assert_equivalent("flip", [0.50001], [0.49999])
