"""R9-025a external smoke — one-shot ``POST /v1/tts`` + ``POST /v1/stt`` live.

🟦 **Operator-pass disposition.** Marked ``@pytest.mark.external`` and
skipped without BOTH ``PERSONA_TTS_API_KEY`` and ``PERSONA_STT_API_KEY``
(Spec 02 D-02-11 convention, mirrors ``test_real_tts_smoke.py`` /
``test_real_provider_smoke.py``). Default ``pytest`` does NOT run it;
operators invoke via ``pytest -m external`` (provider-adapter live-leg rule
— the unit suite proves transport/error-mapping with scripted providers;
this file proves the real signature/response shapes + a genuine round trip,
never run in CI, no live spend on every push).

Drives the ACTUAL FastAPI routes (not the backend classes directly — those
already have their own T14/T12 external smoke coverage) via ``TestClient``
against ``build_app`` in the community (no-auth) edition, so the only
variable under test is the R9-025a REST surface itself:

1. ``POST /v1/tts`` with real text + a real Cartesia voice id returns a
   playable WAV clip (RIFF/WAVE header + non-trivial byte length).
2. That WAV clip fed back through ``POST /v1/stt`` (multipart upload)
   transcribes to text containing a recognisable word from the source —
   loose containment, not exact-match, because real ASR is not byte-perfect.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(
        os.environ.get("PERSONA_TTS_API_KEY") is None
        or os.environ.get("PERSONA_STT_API_KEY") is None,
        reason="real Cartesia + Deepgram API keys required; set PERSONA_TTS_API_KEY "
        "+ PERSONA_STT_API_KEY (+ PERSONA_TTS_VOICE_DEFAULT for a real voice id)",
    ),
]

_SPEECH_TEXT = "Hello there, this is a live round trip smoke test."


def _build_client() -> object:
    """The real app, community edition (no auth wall) — env-driven TTS/STT."""
    from fastapi.testclient import TestClient
    from persona_voice.config import VoiceConfig
    from persona_voice.http.app import build_app

    return TestClient(build_app(VoiceConfig(edition="community")))


def test_tts_then_stt_round_trip_against_real_providers() -> None:
    voice_id = os.environ.get("PERSONA_TTS_VOICE_DEFAULT")
    if not voice_id:
        pytest.skip("set PERSONA_TTS_VOICE_DEFAULT to a real Cartesia voice id")
    client = _build_client()

    tts_resp = client.post(  # type: ignore[attr-defined]
        "/v1/tts", json={"text": _SPEECH_TEXT, "voice_id": voice_id}
    )
    assert tts_resp.status_code == 200, tts_resp.text
    assert tts_resp.headers["content-type"] == "audio/wav"
    wav_bytes = tts_resp.content
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"
    assert len(wav_bytes) > 44  # header + real synthesised audio, not silence

    stt_resp = client.post(  # type: ignore[attr-defined]
        "/v1/stt",
        files={"audio": ("speech.wav", wav_bytes, "audio/wav")},
    )
    assert stt_resp.status_code == 200, stt_resp.text
    transcript = stt_resp.json()["transcript"]
    assert transcript.strip() != ""
    lowered = transcript.lower()
    # Loose containment — real ASR punctuation/casing varies; the words
    # round-tripping through synthesis → transcription is the actual proof.
    assert "hello" in lowered or "test" in lowered, (
        f"round trip transcript did not resemble the source text: {transcript!r}"
    )


def test_tts_unknown_voice_id_surfaces_a_clean_502_not_a_crash() -> None:
    """A bogus ``voice_id`` is a provider-side rejection, never a raw 500."""
    client = _build_client()
    resp = client.post(  # type: ignore[attr-defined]
        "/v1/tts",
        json={"text": "hello", "voice_id": "not-a-real-voice-id-r9-025a"},
    )
    assert resp.status_code == 502, resp.text
    assert resp.json()["detail"]["error"] == "tts_provider_error"
