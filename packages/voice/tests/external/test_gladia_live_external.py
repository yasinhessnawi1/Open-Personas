"""Spec V14 T1 external smoke — real Gladia streaming code-switch (owner-run).

🟦 **Operator-pass disposition.** Marked ``@pytest.mark.external`` and SKIPPED
without ``PERSONA_GLADIA_API_KEY`` (the same env-gated convention as
``test_real_provider_smoke.py``). Default ``pytest`` does NOT run it (the
default ``-m 'not integration and not external and not soak'`` deselects it);
zero CI spend. The owner invokes it explicitly against a real Gladia key:

    PERSONA_GLADIA_API_KEY=... uv run --package persona-voice \
        pytest packages/voice/tests/external/test_gladia_live_external.py \
        -m external -o addopts=""

**What it proves (the T0 headline, now through the SHIPPED backend).** The
production :class:`persona_voice.stt.gladia_backend.GladiaStreamingSTT` — not
the T0 probe — transcribes a real code-switched clip end-to-end and survives
the mid-utterance language switch (the D-V14-1 property; the R9-025 fix). It
uses the same offline-generated ``family1_en_no`` clip the T0 POC built
(English → Norwegian, a pair OUTSIDE Deepgram's code-switch set), regenerating
it on demand via the POC's ``generate_clips.py`` if absent.

This is a SMOKE gate, not a WER threshold: it asserts the transcript is
non-empty and contains recognizable content from BOTH language segments (the
structural "did the second language survive" check). Naturalness/WER on real
bilingual speech is the T5 real-speaker leg, not this.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import wave
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(
        os.environ.get("PERSONA_GLADIA_API_KEY") is None,
        reason="real Gladia API key required; set PERSONA_GLADIA_API_KEY",
    ),
]

# The T0 POC clip families live in the gitignored scratch dir; the generator is
# offline (macOS say + afconvert) so the owner can regenerate on demand.
_POC_DIR = Path(__file__).resolve().parents[4] / ".superpowers" / "sdd" / "v14-poc"
_CLIP = _POC_DIR / "clips" / "family1_en_no.wav"
_GENERATOR = _POC_DIR / "clips" / "generate_clips.py"

_SAMPLE_RATE_HZ = 16_000
_CHUNK_MS = 200
_CHUNK_BYTES = int(_SAMPLE_RATE_HZ * (_CHUNK_MS / 1000.0) * 2)  # PCM16 mono


def _ensure_clip() -> bytes:
    """Return the family1_en_no PCM frames, regenerating the clip if missing."""
    if not _CLIP.exists() and _GENERATOR.exists():
        subprocess.run([sys.executable, str(_GENERATOR)], check=True, cwd=str(_GENERATOR.parent))
    if not _CLIP.exists():
        pytest.skip(f"code-switched clip not available at {_CLIP} (run generate_clips.py)")
    with wave.open(str(_CLIP), "rb") as wf:
        assert wf.getframerate() == _SAMPLE_RATE_HZ
        return wf.readframes(wf.getnframes())


@pytest.mark.asyncio
async def test_gladia_streaming_survives_en_to_no_code_switch() -> None:
    """The shipped Gladia backend transcribes an English→Norwegian clip with
    BOTH segments present — the mid-utterance switch survives (D-V14-1)."""
    from persona_voice.stt.config import StreamingSTTConfig
    from persona_voice.stt.gladia_backend import GladiaStreamingSTT
    from persona_voice.stt.protocol import StreamingSTT

    pcm = _ensure_clip()
    config = StreamingSTTConfig(
        provider="gladia",
        gladia_api_key=os.environ["PERSONA_GLADIA_API_KEY"],  # type: ignore[arg-type]
    )
    backend: StreamingSTT = GladiaStreamingSTT(config)

    finals: list[str] = []

    async def _drain() -> None:
        async for transcript in backend.transcripts():
            if transcript.is_final and transcript.text:
                finals.append(transcript.text)

    drain_task = asyncio.create_task(_drain())
    try:
        offset = 0
        while offset < len(pcm):
            await backend.push_audio(pcm[offset : offset + _CHUNK_BYTES], _SAMPLE_RATE_HZ)
            offset += _CHUNK_BYTES
            await asyncio.sleep(_CHUNK_MS / 1000.0)  # pace ~real-time
        await backend.close()
        await asyncio.wait_for(drain_task, timeout=30.0)
    finally:
        if not drain_task.done():
            drain_task.cancel()
        await backend.close()

    transcript = " ".join(finals).lower()
    if not transcript:
        pytest.fail(
            "gladia returned an EMPTY transcript for the code-switched clip — "
            "falsification: re-verify the /v2/live session shape at docs.gladia.io "
            "(the V2 API has renamed params across changelogs)"
        )
    # Structural "both languages survived" smoke check: an English marker from
    # segment 1 AND a Norwegian marker from segment 2 (tolerant — synthetic-TTS
    # pronunciation is not a native speaker; real-speech WER is the T5 leg).
    _en_markers = ("schedule", "tomorrow", "checking")
    _no_markers = ("møte", "møtet", "klokken", "tre", "flytte")
    has_english = any(word in transcript for word in _en_markers)
    has_norwegian = any(word in transcript for word in _no_markers)
    assert has_english, f"english segment 1 not recognized in: {transcript!r}"
    assert has_norwegian, (
        f"norwegian segment 2 not recognized in: {transcript!r} — the code-switch "
        "did not survive (this is exactly the R9-025 failure V14 fixes)"
    )
