"""The pure served-provider voice pricing helper (Spec M3, T6a-core).

Proves each served-provider branch prices at its OWN registry rate (the
model-fallback attribution property): Gladia vs Deepgram STT, ElevenLabs
Flash/Multilingual vs Cartesia TTS, LiveKit infra. Pure math — no I/O, no DB.
"""

from __future__ import annotations

import pytest
from persona.billing import voice_pricing
from persona.billing.formula import BillingConfig
from persona.billing.pricing_registry import PRICING_ROWS, PricingRow
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


# ----- TTS: ElevenLabs and Cartesia, both char-metered -----------------------


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

    @pytest.mark.parametrize("chars", [1, 1234, 2000, 50_000])
    def test_cartesia_is_char_metered_at_five_cents_per_1k(self, chars: int) -> None:
        # R9-235: Sonic bills about 1 credit per character, priced like ElevenLabs at the
        # Pro plan rate of $0.05 per 1,000 chars, so N chars cost N / 1000 x 5.0 cents.
        cents, basis = voice_tts_cents("cartesia", chars=chars)
        assert cents == pytest.approx(chars / 1000 * 5.0)
        assert basis == "provider_meter"

    def test_cartesia_is_priced_with_the_served_model_name(self) -> None:
        # The backend serves ``sonic-3.5``, which matches no SKU; the lone Cartesia row
        # still prices it, so a real Cartesia turn is never left at zero.
        cents, _ = voice_tts_cents("cartesia", chars=1000, model="sonic-3.5")
        assert cents == pytest.approx(5.0)

    def test_cartesia_costs_the_same_as_elevenlabs_flash_per_char(self) -> None:
        cartesia, _ = voice_tts_cents("cartesia", chars=1234)
        flash, _ = voice_tts_cents("elevenlabs", chars=1234, model="eleven_flash_v2_5")
        assert cartesia == pytest.approx(flash)

    def test_cartesia_zero_chars_is_zero(self) -> None:
        assert voice_tts_cents("cartesia", chars=0) == (0.0, "provider_meter")

    def test_zero_chars_is_zero(self) -> None:
        assert voice_tts_cents("elevenlabs", chars=0) == (0.0, "provider_meter")

    def test_unknown_provider_is_unpriced(self) -> None:
        assert voice_tts_cents("coqui", chars=2000) == (0.0, "unpriced")

    def test_a_non_char_row_is_unpriced_not_priced_per_minute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # R9-235: the meter only ever hands TTS characters, so a per-minute TTS row can
        # never be priced from what it is given. It must say ``unpriced`` (the credit
        # floor still charges) instead of quietly pricing a duration nobody measured.
        per_minute = PricingRow(
            surface="voice_tts",
            provider="cartesia",
            sku="sonic",
            unit="min",
            provider_cost_cents=3.0,
            infra_unit="none",
            cost_basis="provider_meter",
        )
        monkeypatch.setattr(voice_pricing, "_served_row", lambda *_a: per_minute)
        assert voice_tts_cents("cartesia", chars=2000) == (0.0, "unpriced")


def test_every_voice_tts_row_is_char_metered() -> None:
    # The meter passes only characters for TTS, so every TTS row must be priced per
    # 1k chars; a per-minute TTS row is exactly the R9-235 charge of zero.
    tts_rows = [row for row in PRICING_ROWS if row.surface == "voice_tts"]
    assert {row.provider for row in tts_rows} >= {"elevenlabs", "cartesia"}
    assert all(row.unit == "1k chars" for row in tts_rows), [
        (row.provider, row.sku, row.unit) for row in tts_rows
    ]


def test_cartesia_tts_row_is_the_owner_pro_plan_rate() -> None:
    (row,) = [r for r in PRICING_ROWS if r.surface == "voice_tts" and r.provider == "cartesia"]
    assert row.sku == "sonic"
    assert row.unit == "1k chars"
    assert row.provider_cost_cents == pytest.approx(5.0)  # $5 per 100K credits
    assert row.cost_basis == "provider_meter"


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
    tts2, _ = voice_tts_cents("cartesia", chars=1000)  # 1000 chars x 5.0 cents/1k = 5.0
    infra, _ = livekit_infra_cents(cfg, minutes=2.0)  # 2 min × 1.0 = 2.0

    total = stt1 + tts1 + stt2 + tts2 + infra
    assert total == pytest.approx(1.25 + 5.0 + 0.77 + 5.0 + 2.0)
    # The fallback turn is strictly cheaper on STT than the primary turn, proving each
    # segment used its OWN served rate, not a blended one. Cartesia and ElevenLabs Flash
    # share a per-char rate (R9-235), so the TTS arms are equal and both non-zero.
    assert stt2 < stt1
    assert tts2 == pytest.approx(tts1)
    assert tts2 > 0.0
