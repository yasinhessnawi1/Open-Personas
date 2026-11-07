"""The Silero v5.1 runner on the onnxruntime package (``persona_voice.stt.silero_onnx``).

It replaces ``silero-vad-lite`` (no Linux aarch64 wheel; its source build downloads an
x86_64 ONNX Runtime) with the same model run through ``onnxruntime``, which publishes
wheels for Linux x86_64 and aarch64, macOS arm64 and Windows. Numerical equivalence with
the old library is ``test_silero_equivalence.py``; this file pins the contract around it:
the exact model, the window sizes, input validation, one ONNX thread, and reset.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import numpy as np
import pytest
from persona_voice.stt.silero_onnx import (
    SILERO_MODEL_SHA256,
    SileroModelError,
    SileroVAD,
    bundled_model_path,
)

#: Silero v5.1, snakers4/silero-vad tag v5.1, ``src/silero_vad/data/silero_vad.onnx``
#: (git blob b3e3a900c0d70e67b5e2b90a33ad856ee7947930). The same file silero-vad-lite 0.3.0
#: bundled, so the thresholds tuned against it still hold.
_UPSTREAM_V5_1_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"


def _noise(samples: int, seed: int) -> np.ndarray:
    return np.random.default_rng(seed).uniform(-0.5, 0.5, samples).astype(np.float32)


def test_the_shipped_model_is_silero_v5_1_byte_for_byte() -> None:
    assert SILERO_MODEL_SHA256 == _UPSTREAM_V5_1_SHA256
    digest = hashlib.sha256(bundled_model_path().read_bytes()).hexdigest()
    assert digest == _UPSTREAM_V5_1_SHA256


def test_the_model_ships_with_its_mit_licence() -> None:
    notice = (bundled_model_path().parent / "SILERO_VAD_LICENSE").read_text(encoding="utf-8")
    assert notice.startswith("MIT License")
    assert "Copyright (c) 2020-present Silero Team" in notice


def test_a_model_that_is_not_the_pinned_one_is_refused(tmp_path: Path) -> None:
    tampered = bytearray(bundled_model_path().read_bytes())
    tampered[len(tampered) // 2] ^= 0xFF
    path = tmp_path / "silero_vad.onnx"
    path.write_bytes(bytes(tampered))
    with pytest.raises(SileroModelError, match="sha256"):
        SileroVAD(16_000, model_path=path)


@pytest.mark.parametrize(("rate", "window"), [(16_000, 512), (8_000, 256)])
def test_the_window_is_32_ms_at_each_supported_rate(rate: int, window: int) -> None:
    vad = SileroVAD(rate)
    assert vad.sample_rate == rate
    assert vad.window_size_samples == window


def test_an_unsupported_rate_is_refused() -> None:
    with pytest.raises(ValueError, match="16000 or 8000"):
        SileroVAD(44_100)


def test_a_window_of_the_wrong_length_is_refused() -> None:
    vad = SileroVAD(16_000)
    with pytest.raises(ValueError, match="512"):
        vad.process(np.zeros(511, dtype=np.float32))
    with pytest.raises(ValueError, match="512"):
        vad.process(np.zeros(513, dtype=np.float32).tobytes())


def test_bytes_and_arrays_score_the_same() -> None:
    frame = _noise(512, seed=1)
    assert SileroVAD(16_000).process(frame) == SileroVAD(16_000).process(frame.tobytes())


def test_scores_are_probabilities() -> None:
    vad = SileroVAD(16_000)
    scores = [vad.process(_noise(512, seed=i)) for i in range(5)]
    assert all(isinstance(s, float) and 0.0 <= s <= 1.0 for s in scores)


def test_the_session_runs_on_one_thread() -> None:
    # One intra-op and one inter-op thread, as silero-vad-lite configured its C++ session:
    # a VAD window is tiny, and a thread pool per call would compete with the event loop.
    options = SileroVAD(16_000).session_options()
    assert options.intra_op_num_threads == 1
    assert options.inter_op_num_threads == 1


def test_reset_starts_an_independent_stream_exactly_like_a_fresh_model() -> None:
    first, second = _noise(512 * 6, seed=7), _noise(512 * 6, seed=8)
    reused = SileroVAD(16_000)
    for start in range(0, first.size, 512):
        reused.process(first[start : start + 512])
    reused.reset()
    fresh = SileroVAD(16_000)
    for start in range(0, second.size, 512):
        window = second[start : start + 512]
        assert reused.process(window) == fresh.process(window)


def test_the_model_carries_state_between_windows() -> None:
    # Silero is recurrent and needs the previous window's tail as context: the same window
    # after different audio must score differently, or state and context are being dropped.
    window = _noise(512, seed=3)
    after_silence = SileroVAD(16_000)
    after_silence.process(np.zeros(512, dtype=np.float32))
    after_noise = SileroVAD(16_000)
    after_noise.process(_noise(512, seed=4))
    assert after_silence.process(window) != after_noise.process(window)


def test_voice_needs_no_silero_vad_lite() -> None:
    assert importlib.util.find_spec("silero_vad_lite") is None
