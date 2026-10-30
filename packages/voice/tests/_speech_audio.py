"""Real speech for the tests that run the Silero model: a slice of the V8 corpus.

A pure tone is not speech, and the Silero tests used to feed one. ``silero-vad-lite``
before 0.3 dropped the model's audio context between windows, which happened to score a
440 Hz sine at about 0.1, so a 0.05 threshold read it as speech. With the context
restored (0.3, as upstream Silero does it) the tone scores about 0.002 from its third
window: the old tests were exercising the bug, not the model.

The slice comes from ``fixtures/v8_corpus/normal.raw``, real speech rendered by
``fixtures/v8_corpus/generate_corpus.py`` (16 kHz mono PCM16 little-endian). From
192 ms in, every 32 ms window scores at least 0.5 (the production threshold) for more
than 2.7 s, on silero-vad-lite 0.2.1, 0.3.0 and 0.4.0 alike.
"""

from __future__ import annotations

from pathlib import Path

SAMPLE_RATE_HZ = 16_000
_BYTES_PER_SAMPLE = 2
_SPEECH = Path(__file__).resolve().parent / "fixtures" / "v8_corpus" / "normal.raw"
_SPEECH_START_MS = 192
LONGEST_SPEECH_MS = 2_500


def _bytes_for(duration_ms: int) -> int:
    return SAMPLE_RATE_HZ * duration_ms // 1000 * _BYTES_PER_SAMPLE


def speech_pcm(duration_ms: int) -> bytes:
    """``duration_ms`` of continuous real speech as PCM16 little-endian bytes."""
    if not 0 < duration_ms <= LONGEST_SPEECH_MS:
        msg = f"speech_pcm offers 1 to {LONGEST_SPEECH_MS} ms of continuous speech"
        raise ValueError(msg)
    start = _bytes_for(_SPEECH_START_MS)
    return _SPEECH.read_bytes()[start : start + _bytes_for(duration_ms)]
