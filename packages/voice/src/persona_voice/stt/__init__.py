"""Spec V2 streaming-STT package surface.

Public types V2 callers depend on:

* :class:`SpeechActivityEvent` / :class:`SpeechStartedEvent` /
  :class:`SpeechEndedEvent` — sensor-side boundary records emitted by the
  V2 Silero VAD adapter (T05) on the :class:`SpeechActivityListener`
  port (T03). Sensor-vs-decider Protocol boundary per
  D-V2-X-activity-listener-shape (research.md R-V2-2 + Pipecat issue
  #1323 production-bug precedent).
* :class:`STTError` (+ subclasses) — domain-exception hierarchy
  mirroring Spec 02's :class:`persona.backends.errors.ProviderError`
  shape (errors.py:30-75). Backends raise these at construction
  (fail-fast on missing API key) and at call time (mapping provider
  exceptions to domain exceptions at the adapter boundary).
* :class:`Transcript` — re-exported from
  :mod:`persona_voice.loop.streaming` (V1 ships the provider-independent
  record at streaming.py:86-99; V2 callers consume it verbatim — no
  shape change for v0.1 per D-V2-X-transcript-event-kind v0.2 deferral).

T03 lands :class:`StreamingSTT` Protocol + :class:`StreamingSTTConfig`
(``env_prefix="PERSONA_STT_"``) + ``load_streaming_stt`` dispatcher on
top of this base; T04 lands the concrete Deepgram backend per
D-V2-1 LOCK; T05 lands the Silero VAD adapter per
D-V2-X-silero-implementation-shape LOCK.
"""

from __future__ import annotations

from persona_voice.loop.streaming import Transcript
from persona_voice.stt._factory import load_streaming_stt
from persona_voice.stt.config import Provider, StreamingSTTConfig, VADLibrary
from persona_voice.stt.errors import (
    STTAudioFormatError,
    STTAuthenticationError,
    STTError,
    STTRateLimitError,
    STTStreamFailureError,
)
from persona_voice.stt.protocol import SpeechActivityListener, StreamingSTT
from persona_voice.stt.types import (
    SpeechActivityEvent,
    SpeechEndedEvent,
    SpeechStartedEvent,
)

# NOTE: neither the concrete `DeepgramStreamingSTT` backend NOR the R9-025a
# one-shot `transcribe_prerecorded` function are re-exported here — mirrors
# the existing discipline that only the Protocol/config/errors/factory are
# part of this package's public surface (consumers go through
# `load_streaming_stt`, never a concrete provider class), AND keeps this
# package importable without the `deepgram-sdk` extra resolved (T04 lazy-
# import discipline, `_factory.py`). Callers that need the one-shot REST leg
# (`persona_voice.http.app`) import `transcribe_prerecorded` directly from
# `persona_voice.stt.deepgram_backend`.

__all__ = [
    "Provider",
    "SpeechActivityEvent",
    "SpeechActivityListener",
    "SpeechEndedEvent",
    "SpeechStartedEvent",
    "STTAudioFormatError",
    "STTAuthenticationError",
    "STTError",
    "STTRateLimitError",
    "STTStreamFailureError",
    "StreamingSTT",
    "StreamingSTTConfig",
    "Transcript",
    "VADLibrary",
    "load_streaming_stt",
]
