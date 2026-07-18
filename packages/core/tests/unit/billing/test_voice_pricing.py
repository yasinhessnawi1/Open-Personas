"""The pure served-provider voice pricing helper (Spec M3, T6a-core).

Proves each served-provider branch prices at its OWN registry rate (the
model-fallback attribution property): Gladia vs Deepgram STT, ElevenLabs
Flash/Multilingual vs Cartesia TTS, LiveKit infra. Pure math — no I/O, no DB.
"""

from __future__ import annotations

import pytest
from persona.billing.formula import BillingConfig
from persona.billing.voice_pricing import (
    livekit_infra_cents,
    voice_stt_cents,
    voice_tts_cents,
)

# ----- STT: served provider priced at its own ¢/min --------------------------


class TestVoiceSttCents:
    def test_gladia_primary_rate(self) -> None:
        # 120 streamed sec = 2.0 min × 1.25¢/min = 2.5¢.
        cents, basis = voice_stt_cents("gladia", streamed_seconds=120.0)
        assert cents == pytest.approx(2.5)
        assert basis == "provider_meter"

    def test_deepgram_fallback_rate(self) -> None:
        # Same audio, fallback provider → priced at Deepgram's 0.77¢/min.
        cents, basis = voice_stt_cents("deepgram", streamed_seconds=120.0)
        assert cents == pytest.approx(1.54)
        assert basis == "provider_meter"

    def test_fallback_is_cheaper_than_primary_for_the_same_audio(self) -> None:
        gladia, _ = voice_stt_cents("gladia", streamed_seconds=300.0)
        deepgram, _ = voice_stt_cents("deepgram", streamed_seconds=300.0)
        assert deepgram < gladia  # attribution matters: the served rate is what's charged

    def test_zero_audio_is_zero(self) -> None:
        assert voice_stt_cents("gladia", streamed_seconds=0.0) == (0.0, "provider_meter")

    def test_negative_audio_clamps_to_zero(self) -> None:
        cents, _ = voice_stt_cents("gladia", streamed_seconds=-50.0)
        assert cents == 0.0

    def test_unknown_provider_is_unpriced(self) -> None:
        assert voice_stt_cents("whisper-local", streamed_seconds=120.0) == (0.0, "unpriced")


# ----- TTS: ElevenLabs char-metered, Cartesia per-min ------------------------


class TestVoiceTtsCents:
    def test_elevenlabs_flash_char_rate(self) -> None:
        # 2000 chars × 5.0¢/1k = 10.0¢ (Flash, the default model).
        cents, basis = voice_tts_cents("elevenlabs", chars=2000, model="eleven_flash_v2_5")
        assert cents == pytest.approx(10.0)
        assert basis == "provider_meter"

    def test_elevenlabs_multilingual_char_rate(self) -> None:
        # 2000 chars × 10.0¢/1k = 20.0¢ (Multilingual quality tier).
        cents, basis = voice_tts_cents("elevenlabs", chars=2000, model="eleven_multilingual_v2")
        assert cents == pytest.approx(20.0)
        assert basis == "provider_meter"

    def test_elevenlabs_unknown_model_defaults_to_cheapest(self) -> None:
        # Ambiguous model → the cheapest priced row (Flash) so we never over-charge.
        cents, _ = voice_tts_cents("elevenlabs", chars=2000, model=None)
        assert cents == pytest.approx(10.0)  # Flash rate, not Multilingual's 20.0

    def test_cartesia_fallback_is_per_minute_on_audio_seconds(self) -> None:
        # Cartesia (fallback) is per-min: 90 audio-sec = 1.5 min × 3.0¢/min = 4.5¢.
        # Chars are ignored for a per-minute provider.
        cents, basis = voice_tts_cents("cartesia", chars=9999, audio_seconds=90.0)
        assert cents == pytest.approx(4.5)
        assert basis == "provider_meter"

    def test_cartesia_without_audio_seconds_is_zero(self) -> None:
        cents, _ = voice_tts_cents("cartesia", chars=2000)
        assert cents == 0.0

    def test_zero_chars_is_zero(self) -> None:
        assert voice_tts_cents("elevenlabs", chars=0) == (0.0, "provider_meter")

    def test_unknown_provider_is_unpriced(self) -> None:
        assert voice_tts_cents("coqui", chars=2000) == (0.0, "unpriced")


# ----- LiveKit transport: infra-flat per minute ------------------------------


class TestLivekitInfraCents:
    def test_infra_flat_default_rate(self) -> None:
        # Default PERSONA_INFRA_RATE_PER_VOICE_MIN_CENTS = 1.0 → 3 min = 3.0¢.
        cents, basis = livekit_infra_cents(BillingConfig(), minutes=3.0)
        assert cents == pytest.approx(3.0)
        assert basis == "infra_flat"

    def test_honours_configured_rate(self) -> None:
        cfg = BillingConfig(infra_rate_per_voice_min_cents=2.5)
        cents, _ = livekit_infra_cents(cfg, minutes=4.0)
        assert cents == pytest.approx(10.0)

    def test_zero_minutes_is_zero(self) -> None:
        cents, basis = livekit_infra_cents(BillingConfig(), minutes=0.0)
        assert cents == 0.0
        assert basis == "infra_flat"

    def test_negative_minutes_clamps_to_zero(self) -> None:
        cents, _ = livekit_infra_cents(BillingConfig(), minutes=-2.0)
        assert cents == 0.0


# ----- attribution property: a full scripted call sums to the served rates ---


def test_scripted_call_sums_served_provider_costs_plus_livekit_infra() -> None:
    """A whole call's rows = STT(served) + TTS(served) + LiveKit infra/min.

    Two turns: turn 1 served by the PRIMARIES (Gladia + ElevenLabs Flash), turn 2
    by the FALLBACKS (Deepgram + Cartesia). Each segment is priced at whichever
    provider served it — the T6a attribution contract, at the pure-helper level.
    """
    cfg = BillingConfig()
    # Turn 1 — primaries.
    stt1, _ = voice_stt_cents("gladia", streamed_seconds=60.0)  # 1 min × 1.25 = 1.25
    tts1, _ = voice_tts_cents("elevenlabs", chars=1000, model="eleven_flash_v2_5")  # 5.0
    # Turn 2 — fallbacks (same quantities, priced at the fallback rates).
    stt2, _ = voice_stt_cents("deepgram", streamed_seconds=60.0)  # 1 min × 0.77 = 0.77
    tts2, _ = voice_tts_cents("cartesia", chars=1000, audio_seconds=60.0)  # 1 min × 3.0 = 3.0
    infra, _ = livekit_infra_cents(cfg, minutes=2.0)  # 2 min × 1.0 = 2.0

    total = stt1 + tts1 + stt2 + tts2 + infra
    assert total == pytest.approx(1.25 + 5.0 + 0.77 + 3.0 + 2.0)
    # The fallback turn is strictly cheaper on STT and dearer on TTS than the
    # primary turn — proving each segment used its OWN served rate, not a blended one.
    assert stt2 < stt1
    assert tts2 < tts1  # Cartesia 3.0¢ < ElevenLabs Flash 5.0¢ for this segment
