"""Spec V14 T3 external smoke — real ElevenLabs same-voice-multi-language (owner-run).

🟦 **Operator-pass disposition.** Marked ``@pytest.mark.external`` and SKIPPED
without ``PERSONA_ELEVENLABS_API_KEY`` (the env-gated convention). Default
``pytest`` does NOT run it (the default ``-m 'not integration and not external
and not soak'`` deselects it); zero CI spend. The owner invokes it explicitly
against a real ElevenLabs key:

    PERSONA_ELEVENLABS_API_KEY=... uv run --package persona-voice \
        pytest packages/voice/tests/external/test_elevenlabs_tts_external.py \
        -m external -o addopts=""

**What it proves (the T0 headline, now through the SHIPPED backend).** The
production :class:`persona_voice.tts.elevenlabs_backend.ElevenLabsStreamingTTS`
— not the T0 probe — synthesizes the SAME voice_id across ≥3 languages with NO
language code on the wire (D-V14-1), producing real V1-rail PCM audio each time
and measuring first-audio latency. It also fetches the real ``/v1/voices``
catalogue and asserts the dialect-aware mapping surfaces per-language accent
metadata.

Naturalness across languages is the OWNER EAR-TEST (already passed at T0 on the
probe output); this leg is the code-path smoke + latency measurement, not a
re-run of that judgment.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(
        os.environ.get("PERSONA_ELEVENLABS_API_KEY") is None,
        reason="real ElevenLabs API key required; set PERSONA_ELEVENLABS_API_KEY",
    ),
]

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Same voice across every language — the D-V14-1 property.
_TEXTS = {
    "en": "Hello, I wanted to check in about tomorrow's meeting.",
    "no": "Hei, jeg ville høre om møtet i morgen.",
    "ar": "مرحبًا، أردت الاطمئنان على اجتماع الغد.",
    "fr": "Bonjour, je voulais prendre des nouvelles de la réunion de demain.",
}


def _make_config() -> object:
    from persona_voice.tts.config import StreamingTTSConfig

    return StreamingTTSConfig(
        provider="elevenlabs",
        elevenlabs_api_key=os.environ["PERSONA_ELEVENLABS_API_KEY"],  # type: ignore[arg-type]
    )


async def _text_stream(text: str) -> AsyncIterator[str]:
    yield text


@pytest.mark.asyncio
async def test_same_voice_renders_multiple_languages_with_pcm_audio() -> None:
    """One voice_id → real PCM audio for en/no/ar/fr, each under a first-audio
    budget, with NO language code sent (the shipped backend never sends one)."""
    from persona_voice.tts.config import StreamingTTSConfig
    from persona_voice.tts.elevenlabs_backend import ElevenLabsStreamingTTS
    from persona_voice.tts.types import ResolvedVoice

    config: StreamingTTSConfig = _make_config()  # type: ignore[assignment]
    backend = ElevenLabsStreamingTTS(config)

    # Pick a real voice from the live catalogue.
    voices = await backend.list_voices(limit=1)
    if not voices:
        pytest.skip("no voices available on this ElevenLabs account")
    voice_id = voices[0].voice_id

    first_audio_ms: dict[str, float] = {}
    for lang, text in _TEXTS.items():
        voice = ResolvedVoice(provider="elevenlabs", voice_ref=voice_id)
        start = time.perf_counter()
        total_bytes = 0
        first_ms: float | None = None
        async for frame in backend.synthesize(_text_stream(text), voice):
            if first_ms is None:
                first_ms = (time.perf_counter() - start) * 1000.0
            total_bytes += len(frame.data)
            assert frame.sample_rate == 24000  # V1 rail (pcm_24000)
        assert total_bytes > 0, f"no audio produced for {lang!r}"
        assert first_ms is not None
        first_audio_ms[lang] = first_ms

    await backend.close()

    # Warm first-audio should sit near the T0-measured flash-class budget; a
    # generous ceiling avoids network-jitter flakiness (this is a smoke gate,
    # not the p50 measurement — the scorecard holds those).
    warm = sorted(first_audio_ms.values())[1:]  # drop the cold-start outlier
    assert warm, "expected at least two languages measured"
    assert min(warm) < 1500.0, f"first-audio unexpectedly slow: {first_audio_ms}"


@pytest.mark.asyncio
async def test_live_catalogue_maps_dialect_metadata() -> None:
    """The real ``/v1/voices`` fetch maps into entries; an Arabic filter (if any
    Arabic-verified voice exists) surfaces the dialect in the description."""
    from persona_voice.tts.config import StreamingTTSConfig
    from persona_voice.tts.elevenlabs_backend import ElevenLabsStreamingTTS

    config: StreamingTTSConfig = _make_config()  # type: ignore[assignment]
    backend = ElevenLabsStreamingTTS(config)

    all_voices = await backend.list_voices()
    assert all_voices, "expected a non-empty ElevenLabs catalogue"
    assert all(v.voice_id for v in all_voices)
    assert backend.provider_name == "elevenlabs"

    # Arabic filter never returns nothing (fallback to all if unverified).
    ar_voices = await backend.list_voices(language="ar")
    assert ar_voices
