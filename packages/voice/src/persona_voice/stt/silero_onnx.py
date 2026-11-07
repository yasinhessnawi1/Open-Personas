"""Silero VAD v5.1 on the ``onnxruntime`` package: one streaming model, every platform.

Voice used ``silero-vad-lite`` 0.3.0: the Silero v5.1 model inside a small C++ wrapper over a
statically linked ONNX Runtime 1.19. It has no Linux aarch64 wheel, and its source build
hardcodes an x86_64 ONNX Runtime download, so voice could not install on arm64 (the
community edition's Raspberry Pi, Ampere and Graviton hosts). This module runs the SAME
model file through ``onnxruntime``, which publishes wheels for Linux x86_64 and aarch64,
macOS arm64 and Windows, with the same streaming algorithm as the wrapper it replaces
(``silero_vad.cpp`` in silero-vad-lite, itself Silero's v5.1 ``OnnxWrapper``):

* each call takes exactly one 32 ms window (512 samples at 16 kHz, 256 at 8 kHz) of float32
  PCM in [-1, 1];
* the model input is the previous window's last 64 samples (32 at 8 kHz) followed by the
  window; the first window gets zeros;
* the recurrent state (2, 1, 128) carries from call to call; :meth:`SileroVAD.reset` zeroes
  state and context for an independent stream without reloading the model.

Equivalence with silero-vad-lite 0.3.0 is a test, not a hope:
``tests/unit/stt/test_silero_equivalence.py`` holds every window of the V8 corpus within
1e-4 of the old library's scores, with identical decisions at thresholds 0.3, 0.5 and 0.7,
and CI runs it on x86_64 and aarch64.

**The model.** ``data/silero_vad.onnx`` is Silero VAD v5.1 from snakers4/silero-vad, tag
``v5.1``, ``src/silero_vad/data/silero_vad.onnx`` (git blob
``b3e3a900c0d70e67b5e2b90a33ad856ee7947930``), byte for byte the file silero-vad-lite 0.3.0
bundled. MIT licensed, Copyright (c) 2020-present Silero Team; the licence ships beside it as
``data/SILERO_VAD_LICENSE``. The file is verified against :data:`SILERO_MODEL_SHA256` every
time a model loads, and the verified bytes are the ones handed to ONNX Runtime.
"""

from __future__ import annotations

import hashlib
from importlib.resources import as_file, files
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import onnxruntime as ort
from persona.errors import PersonaError

if TYPE_CHECKING:
    import numpy.typing as npt

__all__ = [
    "SILERO_MODEL_SHA256",
    "SILERO_MODEL_SOURCE",
    "SileroModelError",
    "SileroVAD",
    "bundled_model_path",
]

#: sha256 of Silero VAD v5.1's ``silero_vad.onnx``. A different file is refused: the voice
#: thresholds were tuned against this model, and Silero v6 scores speech differently.
SILERO_MODEL_SHA256 = "2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f"

#: Where the bundled model comes from (MIT, Silero Team).
SILERO_MODEL_SOURCE = (
    "https://github.com/snakers4/silero-vad/blob/v5.1/src/silero_vad/data/silero_vad.onnx"
)

_SUPPORTED_RATES = (16_000, 8_000)
_STATE_SHAPE = (2, 1, 128)
_OUTPUT_NAMES = ["output", "stateN"]


class SileroModelError(PersonaError):
    """The Silero model file is not the pinned v5.1 model, or could not be loaded."""


def bundled_model_path() -> Path:
    """The path of the Silero v5.1 model shipped inside this package."""
    resource = files("persona_voice.stt").joinpath("data", "silero_vad.onnx")
    with as_file(resource) as path:
        return Path(path)


def _verified_model_bytes(path: Path) -> bytes:
    """Read the model and refuse it unless its sha256 is the pinned v5.1 digest."""
    try:
        model = path.read_bytes()
    except OSError as exc:
        raise SileroModelError(
            f"cannot read the Silero model at {path}", context={"path": str(path)}
        ) from exc
    digest = hashlib.sha256(model).hexdigest()
    if digest != SILERO_MODEL_SHA256:
        raise SileroModelError(
            f"the Silero model at {path} has sha256 {digest}, expected {SILERO_MODEL_SHA256} "
            "(Silero VAD v5.1)",
            context={"path": str(path), "sha256": digest},
        )
    return model


def _session_options() -> ort.SessionOptions:
    """One intra-op and one inter-op thread, full graph optimisation (the wrapper's settings).

    A window is 576 floats: a thread pool buys nothing and would compete with the voice
    event loop and the other calls' inference for the same cores.
    """
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.log_severity_level = 2  # warnings and above, as the C++ wrapper logged
    return options


class SileroVAD:
    """A streaming Silero v5.1 voice activity model: one score per 32 ms window.

    Same surface as ``silero_vad_lite.SileroVAD``: ``SileroVAD(sample_rate)``,
    :attr:`window_size_samples`, :meth:`process` and :meth:`reset`. Consecutive
    :meth:`process` calls are one continuous stream. One instance must not be used from two
    threads at once (the adapter calls it from one worker thread at a time).
    """

    def __init__(self, sample_rate: int, *, model_path: Path | None = None) -> None:
        """Load and verify the model.

        Args:
            sample_rate: 16000 or 8000.
            model_path: Test seam; the bundled model when omitted. Whatever the path, the
                file must be the pinned v5.1 model.

        Raises:
            ValueError: ``sample_rate`` is not 16000 or 8000.
            SileroModelError: the model is missing, not the pinned file, or fails to load.
        """
        if sample_rate not in _SUPPORTED_RATES:
            msg = f"Sample rate must be 16000 or 8000, got {sample_rate}"
            raise ValueError(msg)
        model = _verified_model_bytes(model_path or bundled_model_path())
        self._options = _session_options()
        try:
            self._session = ort.InferenceSession(
                model, sess_options=self._options, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # noqa: BLE001 (onnxruntime raises its own untyped errors)
            raise SileroModelError(
                "ONNX Runtime could not load the Silero model",
                context={"error": type(exc).__name__},
            ) from exc
        self._sample_rate = sample_rate
        self._window = 32 * (sample_rate // 1000)
        self._context_size = 64 if sample_rate == 16_000 else 32
        self._sr = np.array(sample_rate, dtype=np.int64)
        self._state: npt.NDArray[np.float32] = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context: npt.NDArray[np.float32] = np.zeros(self._context_size, dtype=np.float32)

    @property
    def sample_rate(self) -> int:
        """The sample rate this model was built for."""
        return self._sample_rate

    @property
    def window_size_samples(self) -> int:
        """Samples per :meth:`process` call: 32 ms, so 512 at 16 kHz and 256 at 8 kHz."""
        return self._window

    def session_options(self) -> ort.SessionOptions:
        """The options the ONNX session was built with (for inspection)."""
        return self._options

    def reset(self) -> None:
        """Zero the recurrent state and the carried context: a new, independent stream."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros(self._context_size, dtype=np.float32)

    def process(self, data: bytes | bytearray | memoryview | npt.NDArray[np.float32]) -> float:
        """Score one window: the probability, 0 to 1, that it contains speech.

        Args:
            data: Exactly :attr:`window_size_samples` float32 samples in [-1, 1], mono, at
                :attr:`sample_rate`: a float32 array, or its raw bytes (native byte order,
                as ``ndarray.tobytes()`` gives them).

        Raises:
            ValueError: the window is not exactly :attr:`window_size_samples` samples.
        """
        if isinstance(data, (bytes, bytearray, memoryview)):
            if len(data) % 4:
                msg = "Data length must be a whole number of float32 samples (4 bytes each)"
                raise ValueError(msg)
            window = np.frombuffer(data, dtype=np.float32)
        else:
            window = np.asarray(data, dtype=np.float32)
        if window.ndim != 1 or window.shape[0] != self._window:
            msg = (
                f"Data length must be equal to the window size ({self._window}), got {window.shape}"
            )
            raise ValueError(msg)
        model_input = np.concatenate((self._context, window)).reshape(1, -1)
        output, state = self._session.run(
            _OUTPUT_NAMES, {"input": model_input, "state": self._state, "sr": self._sr}
        )
        self._state = np.asarray(state, dtype=np.float32)
        self._context = model_input[0, -self._context_size :].copy()
        return float(np.asarray(output).reshape(-1)[0])
