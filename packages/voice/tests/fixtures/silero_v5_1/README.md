# Silero v5.1 reference scores (upstream)

`context_v5_1.json` and `sample.wav` are copied unchanged from the silero-vad-lite 0.3.0
source distribution (`tests/fixtures/context_v5_1.json` and `tests/sample.wav`;
sdist sha256 `1afea680ff016d23b757d61ef1294a60470ae1cecfeb3b7124d96ab293d56ccf`, the hash
`uv.lock` pinned). They are MIT licensed, Copyright (c) 2024 David Zurow; the licence is
`LICENSE` in this folder.

The scores are an independent NumPy reference of Silero's own v5.1 `OnnxWrapper` (zero initial
state and context, 64 samples of carried context at 16 kHz and 32 at 8 kHz), run on ONNX
Runtime 1.19.0 against the same model this package ships (sha256
`2623a2953f6ff3d2c1e61740c6cdb7168133479b267dfef114a4a3cc5bdd788f`). The 8 kHz input is every
second sample of `sample.wav`. `tests/unit/stt/test_silero_equivalence.py` holds
`persona_voice.stt.silero_onnx` to them.

| File | sha256 |
|---|---|
| `context_v5_1.json` | `34a880e3a646d7bc58299f346ac339ba6d0a564bd0ff1bac0a5eff0cdda8b6b0` |
| `sample.wav` | `bef32b981a389d3fa04bbc2aa22b57f65653bbc77c6ded188fc9a68275cbf7e8` |
