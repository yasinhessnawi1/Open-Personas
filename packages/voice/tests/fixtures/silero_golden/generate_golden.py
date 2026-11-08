"""Provenance for ``v8_corpus_scores.json``: Silero scores from silero-vad-lite 0.3.0.

On-demand generator, NOT a test. The voice VAD used to run on ``silero-vad-lite`` 0.3.0
(the Silero v5.1 model inside a C++ ONNX Runtime 1.19 wrapper). It now runs the same model
on the ``onnxruntime`` Python package (``persona_voice.stt.silero_onnx``), because
silero-vad-lite has no Linux aarch64 wheel and its source build downloads an x86_64 ONNX
Runtime. ``tests/unit/stt/test_silero_equivalence.py`` holds the new runner to these scores.

The scores are what silero-vad-lite 0.3.0 produced on an x86_64 machine for every 512-sample
window of each V8 corpus file (16 kHz mono PCM16, framed by the production
``SileroFramer``), one fresh model per file. silero-vad-lite is no longer a dependency, so
regenerate with it pulled in for the one run, from the repo root:

    G=packages/voice/tests/fixtures/silero_golden/generate_golden.py
    uv run --with silero-vad-lite==0.3.0 python $G

Regenerating is only ever needed if the corpus changes. The model and the wrapper are frozen:
moving the model (silero-vad-lite 0.4 ships Silero v6.2) is a product decision that rechecks
the thresholds, not a fixture refresh.
"""

from __future__ import annotations

import hashlib
import json
import platform
from importlib.metadata import version
from pathlib import Path

import silero_vad_lite
from persona_voice.stt.vad_silero import SILERO_SAMPLE_RATE_HZ, SileroFramer

_HERE = Path(__file__).resolve().parent
_CORPUS = _HERE.parent / "v8_corpus"
_OUT = _HERE / "v8_corpus_scores.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    assert version("silero-vad-lite") == "0.3.0", version("silero-vad-lite")
    model = Path(silero_vad_lite.__file__).parent / "data" / "silero_vad.onnx"
    scores: dict[str, dict[str, object]] = {}
    for raw in sorted(_CORPUS.glob("*.raw")):
        vad = silero_vad_lite.SileroVAD(SILERO_SAMPLE_RATE_HZ)
        frames = list(SileroFramer().push(raw.read_bytes()))
        scores[raw.name] = {
            "sha256": _sha256(raw),
            "scores": [float(vad.process(frame.tobytes())) for frame in frames],
        }
    fixture = {
        "generator": "packages/voice/tests/fixtures/silero_golden/generate_golden.py",
        "implementation": "silero-vad-lite 0.3.0 (C++ wrapper, ONNX Runtime 1.19.0 static)",
        "machine": f"{platform.system()} {platform.machine()}",
        "model_sha256": _sha256(model),
        "sample_rate_hz": SILERO_SAMPLE_RATE_HZ,
        "window_samples": 512,
        "files": scores,
    }
    _OUT.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {_OUT} ({sum(len(v['scores']) for v in scores.values())} windows)")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
