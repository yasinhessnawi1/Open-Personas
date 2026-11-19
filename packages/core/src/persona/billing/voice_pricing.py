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
* **TTS** - every provider is CHAR-metered (``1k chars``): real synthesized
  characters x cents/1k-char, ElevenLabs and Cartesia alike (R9-235). A TTS row in
  any other unit is ``unpriced``, because characters are all the meter measures.
* **LiveKit transport** — ``infra_flat`` per voice minute (no provider cost).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.billing.pricing_registry import infra_rate_cents, served_row

if TYPE_CHECKING:
    from persona.billing.basis import CostBasis
    from persona.billing.formula import BillingConfig

__all__ = [
    "livekit_infra_cents",
    "voice_stt_cents",
    "voice_tts_cents",
]

_SECONDS_PER_MINUTE = 60.0
#: Every TTS row (and the docs table) prices per 1,000 characters.
_CHARS_PER_UNIT = 1000.0
_TTS_UNIT = "1k chars"


#: The registry lookup both priced surfaces share (promoted to ``pricing_registry`` so voice
#: and image cannot drift into two answers for one price). Kept as a module-local alias so the
#: call sites below read unchanged.
_served_row = served_row


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
    model: str | None = None,
) -> tuple[float, CostBasis]:
    """The served TTS provider's real cost for one synthesized segment.

    Every TTS provider is CHAR-metered: real ``chars`` x cents/1k-char. ElevenLabs
    Flash vs Multilingual is selected by ``model`` (cheapest on ambiguity); Cartesia
    has one Sonic row (R9-235). Unknown provider, or a row not priced per 1k chars
    (nothing here could price it from characters), gives ``(0.0, "unpriced")``.
    """
    row = _served_row("voice_tts", provider, model)
    if row is None or row.provider_cost_cents is None or row.unit != _TTS_UNIT:
        return 0.0, "unpriced"
    return row.provider_cost_cents * (max(0, chars) / _CHARS_PER_UNIT), "provider_meter"


def livekit_infra_cents(config: BillingConfig, *, minutes: float) -> tuple[float, CostBasis]:
    """The LiveKit transport infra-flat cost for ``minutes`` of connected voice.

    Self-hosted → zero provider cost; the charge is purely the per-voice-minute
    infra flat rate (``PERSONA_INFRA_RATE_PER_VOICE_MIN_CENTS``). Basis
    ``infra_flat``.
    """
    rate = infra_rate_cents(config, "voice_min")
    return rate * max(0.0, minutes), "infra_flat"
