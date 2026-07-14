"""Unit tests for :func:`load_streaming_tts` — the V3 backend dispatcher (T03).

Mirrors V2's ``test_factory.py`` discipline. T04 lands the concrete
``cartesia`` branch (D-V3-1 LOCK launch) + extends this file with the
happy-path construction test. At T03 the factory's own logic is fully
exercised by the unknown-provider + not-yet-wired-alternative paths:

1. Unknown / un-wired providers raise :class:`TTSError` with the
   structured ``context`` dict the Spec 02 contract requires.
2. ``elevenlabs`` is a valid Literal (documented alternative behind the
   same Protocol seam, D-V3-1 paragraph 2) but is NOT wired into the
   factory at v0.1 — it must raise :class:`TTSError` until the v0.2
   backend lands.
3. The unknown-provider message enumerates the Literal values for
   operator clarity.
"""

from __future__ import annotations

import os

import pytest
from persona_voice.tts import StreamingTTSConfig, TTSError, load_streaming_tts


@pytest.fixture(autouse=True)
def _strip_persona_tts_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("PERSONA_TTS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("PERSONA_ELEVENLABS_API_KEY", raising=False)


def test_unknown_provider_raises_tts_error() -> None:
    # model_construct bypasses Literal validation to exercise the factory's
    # defensive unknown-provider branch (a future Literal value added but
    # not wired into dispatch).
    config = StreamingTTSConfig.model_construct(provider="cloud-fancy-9000")
    with pytest.raises(TTSError) as exc_info:
        load_streaming_tts(config)
    assert "unknown or unwired TTS provider" in str(exc_info.value)
    assert exc_info.value.context["provider"] == "cloud-fancy-9000"


def test_cartesia_branch_returns_concrete_backend() -> None:
    # T04 swap-in: a valid PERSONA_TTS_API_KEY yields a CartesiaStreamingTTS.
    from persona_voice.tts.cartesia_backend import CartesiaStreamingTTS

    config = StreamingTTSConfig(provider="cartesia", api_key="ct-test-key")
    backend = load_streaming_tts(config)
    assert isinstance(backend, CartesiaStreamingTTS)
    assert backend.provider_name == "cartesia"
    assert backend.model_name == "sonic-3.5"


def test_cartesia_branch_fails_fast_without_api_key() -> None:
    # Spec 02 D-02-10 fail-fast: missing PERSONA_TTS_API_KEY surfaces as
    # TTSAuthenticationError at construction.
    from persona_voice.tts import TTSAuthenticationError

    config = StreamingTTSConfig(provider="cartesia")
    with pytest.raises(TTSAuthenticationError) as exc_info:
        load_streaming_tts(config)
    assert exc_info.value.context["provider"] == "cartesia"


def test_elevenlabs_branch_returns_concrete_backend() -> None:
    # Spec V14 (D-V14-9): the ElevenLabs backend is now wired behind the same
    # Protocol seam. A valid PERSONA_ELEVENLABS_API_KEY yields the concrete
    # backend (supersedes the pre-V14 not-yet-wired assertion).
    from persona_voice.tts.elevenlabs_backend import ElevenLabsStreamingTTS

    config = StreamingTTSConfig(provider="elevenlabs", elevenlabs_api_key="el-test-key")
    backend = load_streaming_tts(config)
    assert isinstance(backend, ElevenLabsStreamingTTS)
    assert backend.provider_name == "elevenlabs"
    assert backend.model_name == "eleven_flash_v2_5"


def test_elevenlabs_branch_fails_fast_without_api_key() -> None:
    # Spec 02 D-02-10 fail-fast: missing PERSONA_ELEVENLABS_API_KEY surfaces as
    # TTSAuthenticationError at construction.
    from persona_voice.tts import TTSAuthenticationError

    config = StreamingTTSConfig(provider="elevenlabs")
    with pytest.raises(TTSAuthenticationError) as exc_info:
        load_streaming_tts(config)
    assert exc_info.value.context["provider"] == "elevenlabs"


def test_unknown_provider_message_lists_alternatives() -> None:
    config = StreamingTTSConfig.model_construct(provider="nonsense")
    with pytest.raises(TTSError) as exc_info:
        load_streaming_tts(config)
    msg = str(exc_info.value)
    assert "cartesia" in msg
    assert "elevenlabs" in msg
