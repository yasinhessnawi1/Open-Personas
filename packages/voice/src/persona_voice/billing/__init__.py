"""Voice per-turn owner billing (Spec M3, T6b-1).

The metering substrate that turns a live voice call's real usage into incremental
owner deducts through the core :class:`~persona.billing.metered.MeteredBilling`
seam (no persona-api hop — latency-critical). See :mod:`persona_voice.billing.turn_meter`.
"""

from __future__ import annotations

from persona_voice.billing.cutoff import VoiceExhaustionCutoff
from persona_voice.billing.turn_meter import (
    TurnUsage,
    VoiceTurnAccumulator,
    VoiceTurnBillingMeter,
)

__all__ = [
    "TurnUsage",
    "VoiceExhaustionCutoff",
    "VoiceTurnAccumulator",
    "VoiceTurnBillingMeter",
]
