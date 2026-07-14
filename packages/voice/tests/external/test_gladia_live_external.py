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


@pytest.mark.asyncio
async def test_gladia_batch_oneshot_transcribes_the_clip() -> None:
    """Spec V14 T2 (D-V14-14): the shipped BATCH one-shot transcriber (the one
    the dictation route dispatches to under ``provider=gladia``) transcribes the
    same clip end-to-end against real Gladia — a code-path smoke.

    NOTE (T5 real-speech gate): T0 found batch code-switch fidelity WORSE than
    streaming on SYNTHETIC clips. This leg only asserts a NON-EMPTY transcript
    with the (English) leading segment present — the HARD real-speech dictation
    fidelity check (does the SECOND language survive batch on real bilingual
    speech?) is owed at T5, per the coordinator's ruling. Do not treat a pass
    here as clearing that gate."""
    from persona_voice.stt.config import StreamingSTTConfig
    from persona_voice.stt.gladia_backend import transcribe_oneshot_batch

    with wave.open(str(_CLIP), "rb") as wf:
        # A minimal WAV wrapper the batch upload can sniff (the real dictation
        # path uploads webm/opus; the batch API accepts both).
        import io

        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(_SAMPLE_RATE_HZ)
            out.writeframes(wf.readframes(wf.getnframes()))
        wav_bytes = buf.getvalue()

    config = StreamingSTTConfig(
        provider="gladia",
        gladia_api_key=os.environ["PERSONA_GLADIA_API_KEY"],  # type: ignore[arg-type]
    )
    transcript = (
        await transcribe_oneshot_batch(wav_bytes, config=config, content_type="audio/wav")
    ).lower()
    if not transcript:
        pytest.fail(
            "gladia BATCH returned an EMPTY transcript — re-verify /v2/upload + "
            "/v2/pre-recorded shapes at docs.gladia.io"
        )
    assert any(word in transcript for word in ("schedule", "tomorrow", "checking")), (
        f"leading english segment not recognized in batch transcript: {transcript!r}"
    )


# ---------------------------------------------------------------------------
# THE HARD real-speech dictation fidelity gate (Spec V14 T5, owner ruling).
# ---------------------------------------------------------------------------
#
# T0's "batch anglicizes/collapses the 2nd language" finding (see
# ``test_gladia_batch_oneshot_transcribes_the_clip`` above) was measured on
# SYNTHETIC (macOS ``say``) clips ONLY. The batch one-shot path is what
# ``/v1/stt`` dispatches dictation to under ``provider=gladia`` (D-V14-14) —
# shipping it for real bilingual DICTATION on the strength of a synthetic-clip
# measurement alone would be unproven. This is that proof's skeleton.
#
# **PASS/FAIL criterion (explicit, per the coordinator's ruling):** a real
# bilingual clip through the batch one-shot path must preserve markers from
# BOTH declared languages in the returned transcript. If the second language
# is anglicized/dropped, this is a FAIL, and the escalation is:
#
#   (A) decoder + WS-fast-drive: decode the container to raw PCM server-side
#       and drive Gladia's LIVE streaming WS (T0 found streaming preserved
#       code-switching cleanly; T1 already ships the streaming backend) —
#       trades one dependency (an audio decoder) for restored fidelity.
#   (B) web-side raw-PCM AudioWorklet capture: capture already-PCM audio in
#       the browser (bypassing MediaRecorder's compressed webm/opus container
#       entirely), so the SAME streaming backend can be driven directly from
#       the client — a web-side dep-gated change, no server decoder needed.
#
# Both are separate, dep-gated decisions — NOT made by this test. This test
# only proves (or disproves) the trigger.
#
# **Why this cannot run in CI**: no synthetic TTS clip is legitimate evidence
# here (that is exactly the T0 caveat this gate exists to close) — it needs a
# REAL bilingual human speaker recording, which is an owner-provided artifact,
# not a repo fixture. Skips cleanly without one; CI never runs it regardless
# (the ``external`` marker + ``PERSONA_GLADIA_API_KEY`` skip already excludes
# it from every automated run).
_REAL_CLIP_ENV = "PERSONA_V14_REAL_BILINGUAL_CLIP"
_REAL_CLIP_LANG1_MARKERS_ENV = "PERSONA_V14_REAL_BILINGUAL_LANG1_MARKERS"
_REAL_CLIP_LANG2_MARKERS_ENV = "PERSONA_V14_REAL_BILINGUAL_LANG2_MARKERS"

# Sensible defaults for the documented default pairing (en -> no, matching
# family1 / the project's own default persona-language pair) — override via
# the env vars above (comma-separated words) for a different real recording.
_DEFAULT_LANG1_MARKERS = ("schedule", "tomorrow", "checking", "meeting", "hello")
_DEFAULT_LANG2_MARKERS = ("møte", "møtet", "klokken", "flytte", "hei", "takk")


@pytest.mark.asyncio
async def test_gladia_batch_oneshot_real_bilingual_speech_fidelity() -> None:
    """THE HARD gate: does the SHIPPED batch one-shot path preserve BOTH
    languages on a REAL bilingual speaker's recording (not a synthetic clip)?

    Owner-run:

        # Record ~5-10s of real bilingual speech (a clean language switch
        # mid-clip, e.g. "Let me check the schedule tomorrow. <switch to
        # Norwegian>") as 16kHz mono PCM16 WAV (or webm/opus — batch accepts
        # both containers), then:
        PERSONA_GLADIA_API_KEY=... \\
        PERSONA_V14_REAL_BILINGUAL_CLIP=/path/to/real_bilingual.wav \\
        uv run --package persona-voice pytest \\
            packages/voice/tests/external/test_gladia_live_external.py \\
            -k real_bilingual_speech_fidelity -m external -o addopts=""

    A different language pair? Override the marker word lists:
        PERSONA_V14_REAL_BILINGUAL_LANG1_MARKERS="hello,thanks"
        PERSONA_V14_REAL_BILINGUAL_LANG2_MARKERS="hola,gracias"
    """
    clip_path_str = os.environ.get(_REAL_CLIP_ENV)
    if not clip_path_str:
        pytest.skip(
            f"{_REAL_CLIP_ENV} not set — this is the HARD real-speech dictation "
            "fidelity gate (Spec V14 T5). A synthetic TTS clip cannot substitute for "
            "it (that is exactly the T0 caveat this gate closes). Record a short "
            "real bilingual clip and set the env var to its path; see this test's "
            "docstring for the exact command."
        )
    clip_path = Path(clip_path_str)
    if not clip_path.exists():
        pytest.fail(f"{_REAL_CLIP_ENV}={clip_path_str!r} does not exist")

    from persona_voice.stt.config import StreamingSTTConfig
    from persona_voice.stt.gladia_backend import transcribe_oneshot_batch

    audio_bytes = clip_path.read_bytes()
    content_type = "audio/wav" if clip_path.suffix.lower() == ".wav" else "audio/webm"
    config = StreamingSTTConfig(
        provider="gladia",
        gladia_api_key=os.environ["PERSONA_GLADIA_API_KEY"],  # type: ignore[arg-type]
    )
    transcript = (
        await transcribe_oneshot_batch(audio_bytes, config=config, content_type=content_type)
    ).lower()

    lang1_markers = (
        tuple(
            w.strip().lower()
            for w in os.environ.get(_REAL_CLIP_LANG1_MARKERS_ENV, "").split(",")
            if w.strip()
        )
        or _DEFAULT_LANG1_MARKERS
    )
    lang2_markers = (
        tuple(
            w.strip().lower()
            for w in os.environ.get(_REAL_CLIP_LANG2_MARKERS_ENV, "").split(",")
            if w.strip()
        )
        or _DEFAULT_LANG2_MARKERS
    )

    print(f"\n[V14 T5 real-speech fidelity] transcript: {transcript!r}")  # owner-visible (-s)

    if not transcript:
        pytest.fail("FAIL — gladia BATCH returned an EMPTY transcript for real bilingual speech")

    has_lang1 = any(w in transcript for w in lang1_markers)
    has_lang2 = any(w in transcript for w in lang2_markers)
    if has_lang1 and has_lang2:
        return  # PASS — both languages survived the batch one-shot path.
    pytest.fail(
        "FAIL — the batch one-shot path did not preserve both languages on REAL "
        f"speech (lang1 markers found={has_lang1}, lang2 markers found={has_lang2}, "
        f"transcript={transcript!r}). This CONFIRMS the T0 synthetic-clip finding on "
        "real speech: escalate to (A) decoder + WS-fast-drive streaming one-shot, or "
        "(B) web-side raw-PCM AudioWorklet capture — see this file's module-level "
        "comment above for the full tradeoff. Do not ship batch dictation as-is."
    )
