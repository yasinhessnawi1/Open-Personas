"""Served-provider voice metering prices (Spec M3, T6a-core — D-M3-R3).

Voice bills the **actually-served** provider at its real per-unit rate, mirroring
model-fallback attribution: whichever backend served a segment is priced at that
provider's rate (Gladia/ElevenLabs primary, Deepgram/Cartesia fallback). There is
no runtime provider-outage failover today — STT/TTS ``config.provider`` *is* the
served provider — so the "fallback rate" is simply what these functions return
when handed ``provider="deepgram"`` / ``"cartesia"``; the runtime wiring (T6b)
passes whichever provider actually served.

Rates are sourced from :data:`~persona.billing.pricing_registry.PRICING_ROWS`
(the one pricing truth). This module is **pure** — it turns a served provider +
a real metered quantity into a provider-cost in cents + its ``cost_basis``; it
does no I/O and holds no state. The per-turn deduct that spends these numbers
through :class:`~persona.billing.metered.MeteredBilling` is T6b's (off the audio
loop). Unknown provider / no priced row → ``(0.0, "unpriced")`` (fail-soft: the
credit floor still applies at the charge site).

Metering units per surface:

* **STT** — ``audio-min``: real streamed audio-seconds (V8 rebase) × the served
  provider's ¢/min.
* **TTS** — ElevenLabs is CHAR-metered (``1k chars``): real synthesized
  characters × ¢/1k-char; Cartesia (fallback) is per-``min`` and is priced on the
  segment's real audio-seconds.
* **LiveKit transport** — ``infra_flat`` per voice minute (no provider cost).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.billing.pricing_registry import PRICING_ROWS, infra_rate_cents

if TYPE_CHECKING:
    from persona.billing.basis import CostBasis
    from persona.billing.formula import BillingConfig
    from persona.billing.pricing_registry import PricingRow

__all__ = [
    "livekit_infra_cents",
    "voice_stt_cents",
    "voice_tts_cents",
]

_SECONDS_PER_MINUTE = 60.0
#: ElevenLabs (and the docs table) price per 1,000 characters.
_CHARS_PER_UNIT = 1000.0


def _served_row(surface: str, provider: str, model: str | None) -> PricingRow | None:
    """The registry row for the served ``(surface, provider)``, disambiguated by ``model``.

    A provider with several priced SKUs (ElevenLabs Flash vs Multilingual) is
    resolved by ``model`` when it matches a row's ``sku``; otherwise the CHEAPEST
    priced row wins, so ambiguity never over-charges. Rows without a priced
    ``provider_cost_cents`` (the LLM resolver rows) are ignored here.
    """
    rows = [
        row
        for row in PRICING_ROWS
        if row.surface == surface
        and row.provider == provider
        and row.provider_cost_cents is not None
    ]
    if not rows:
        return None
    if model is not None:
        for row in rows:
            if row.sku == model:
                return row
    return min(rows, key=lambda row: row.provider_cost_cents or 0.0)


def voice_stt_cents(
    provider: str,
    *,
    streamed_seconds: float,
    model: str | None = None,
) -> tuple[float, CostBasis]:
    """The served STT provider's real cost for ``streamed_seconds`` of audio.

    Priced at the served provider's ¢/min (``voice_stt`` rows: Gladia primary,
    Deepgram fallback) on the REAL streamed audio seconds (V8 rebase), not
    wall-clock. Unknown provider → ``(0.0, "unpriced")``.
    """
    row = _served_row("voice_stt", provider, model)
    if row is None or row.provider_cost_cents is None:
        return 0.0, "unpriced"
    minutes = max(0.0, streamed_seconds) / _SECONDS_PER_MINUTE
    return row.provider_cost_cents * minutes, "provider_meter"


def voice_tts_cents(
    provider: str,
    *,
    chars: int = 0,
    audio_seconds: float | None = None,
    model: str | None = None,
) -> tuple[float, CostBasis]:
    """The served TTS provider's real cost for one synthesized segment.

    ElevenLabs (primary) is CHAR-metered: real ``chars`` × ¢/1k-char (Flash vs
    Multilingual selected by ``model``, cheapest on ambiguity). Cartesia
    (fallback) is per-``min`` and is priced on ``audio_seconds`` (the segment's
    real audio duration). Unknown provider → ``(0.0, "unpriced")``.
    """
    row = _served_row("voice_tts", provider, model)
    if row is None or row.provider_cost_cents is None:
        return 0.0, "unpriced"
    if row.unit == "1k chars":
        return row.provider_cost_cents * (max(0, chars) / _CHARS_PER_UNIT), "provider_meter"
    # A per-minute provider (Cartesia fallback): price the segment's audio minutes.
    minutes = max(0.0, audio_seconds or 0.0) / _SECONDS_PER_MINUTE
    return row.provider_cost_cents * minutes, "provider_meter"


def livekit_infra_cents(config: BillingConfig, *, minutes: float) -> tuple[float, CostBasis]:
    """The LiveKit transport infra-flat cost for ``minutes`` of connected voice.

    Self-hosted → zero provider cost; the charge is purely the per-voice-minute
    infra flat rate (``PERSONA_INFRA_RATE_PER_VOICE_MIN_CENTS``). Basis
    ``infra_flat``.
    """
    rate = infra_rate_cents(config, "voice_min")
    return rate * max(0.0, minutes), "infra_flat"
