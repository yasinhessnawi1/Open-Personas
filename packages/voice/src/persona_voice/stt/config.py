"""Streaming-STT configuration loaded from ``PERSONA_STT_*`` env vars.

:class:`StreamingSTTConfig` is the input to
:func:`persona_voice.stt._factory.load_streaming_stt`. Mirrors the
Spec 02 :class:`persona.backends.config.BackendConfig` shape verbatim
(``config.py:49-84``): :class:`~pydantic_settings.BaseSettings` with
``env_prefix="PERSONA_STT_"`` + ``extra="ignore"`` (Pydantic Settings
convention — tolerates extra env vars in the process environment);
:class:`~pydantic.SecretStr` ``api_key`` so ``repr(config)`` never leaks
it; ``Field`` constraints on every numeric knob so misconfigured
operators fail fast at construction (the Spec 02 D-02-10 +
V2 D-V2-X-cost-discipline precedent).

**Provider Literal.** ``deepgram`` is the D-V2-1 LOCK launch provider.
``speechmatics`` is documented as the alternative-provider story behind
the same :class:`persona_voice.stt.protocol.StreamingSTT` Protocol seam
(D-V2-1 paragraph 2); ``whisper-streaming`` is the v0.2 self-hosted
candidate flagged by the STT per-minute cost cadence review (D-V2-X-cost-discipline).
The Literal pins all three even though T03 ships only Deepgram —
keeping the enumeration honest about what shape the Protocol covers.

**VAD library Literal.** ``silero`` is the D-V2-X-silero-implementation-shape
LOCK primary path; ``webrtc`` is reserved for the v0.2 fallback per
R-V2-2's falsification trigger (Silero P95 onset > 150 ms on actual
deployment CPU forces fallback to WebRTC-VAD with documented quality
regression).

**Field constraints.**

* ``deepgram_endpointing_ms`` (10–2000) — Deepgram's silence-window
  knob for emitting FINAL transcripts; tighter values surface partials
  faster at the cost of more FPs. Default 300 ms per R-V2-1 published
  Nova-3 vendor recommendation.
* ``deepgram_utterance_end_ms`` (100–5000) — secondary "no audio at
  all" guard; the larger of the two thresholds wins. Default 1000 ms.
* ``silero_min_speech_duration_ms`` / ``silero_min_silence_duration_ms``
  / ``silero_activation_threshold`` — Silero VAD tuning knobs per the
  R-V2-2 model parameter survey. Defaults are the model card's
  recommendations for telephony-bandwidth (16 kHz mono) audio.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Provider", "StreamingSTTConfig", "VADLibrary"]

Provider = Literal[
    "deepgram",
    "speechmatics",
    "whisper-streaming",
]

VADLibrary = Literal["silero", "webrtc"]


class StreamingSTTConfig(BaseSettings):
    """Env-driven configuration for a single :class:`StreamingSTT` backend.

    Reads from ``PERSONA_STT_*`` env vars. Mirrors
    :class:`persona.backends.config.BackendConfig` verbatim per the
    Spec 02 mirror discipline (``config.py:49-84``).

    Attributes:
        provider: Which streaming-STT backend to load. ``deepgram`` is
            the D-V2-1 LOCK launch.
        model: Model identifier within the provider. ``nova-3`` is the
            Deepgram launch model per R-V2-1.
        api_key: Provider API key. Stored as
            :class:`~pydantic.SecretStr` so ``repr(config)`` does not
            leak it. Construction-time validation in the concrete
            backend fails fast with
            :class:`persona_voice.stt.errors.STTAuthenticationError`
            when missing (D-02-10 fail-fast precedent).
        base_url: Optional override for the provider's default
            streaming endpoint (proxies, self-hosted endpoints, new
            providers not in the launch set).
        request_timeout_s: HTTP / WebSocket request timeout in seconds.
            Default 60.0 mirrors Spec 02 ``BackendConfig.request_timeout_s``.
        language_hint: Optional ISO-639-1 language code (e.g. ``"en"``,
            ``"no"``, ``"ar"``) the backend may pass to the provider as
            a recognition hint. ``None`` lets the provider auto-detect —
            true for the prerecorded one-shot path
            (``persona_voice.stt.deepgram_backend.transcribe_prerecorded``,
            Deepgram ``detect_language=true``, R9-025 reopen). The live
            WebSocket backend has no such capability (Deepgram's streaming
            API requires a language pinned before the socket opens) and
            falls back to ``"en"``; in practice every call supplies a
            concrete value first via per-call language routing
            (``persona_voice.agent.language.apply_stt_route``, Spec 32),
            which resolves the persona's declared ``identity.language_default``
            — NOT this raw global default, which is a single-value-for-
            the-whole-service stopgap that predates that routing.
        vad_library: Which speech-activity sensor T05 instantiates.
            ``silero`` is the LOCK primary path; ``webrtc`` is the
            v0.2 fallback.
        deepgram_endpointing_ms: Deepgram FINAL-emit silence window in
            ms (10–2000). Default 300.
        deepgram_utterance_end_ms: Deepgram "no audio at all" guard in
            ms (100–5000). Default 1000.
        silero_min_speech_duration_ms: Minimum speech segment Silero
            classifies as voiced (10–500). Default 50.
        silero_min_silence_duration_ms: Minimum silence segment Silero
            classifies as offset (50–2000). Default 200.
        silero_activation_threshold: Silero VAD activation probability
            cutoff (0.0–1.0). Default 0.5 per the model card.
    """

    model_config = SettingsConfigDict(
        env_prefix="PERSONA_STT_",
        extra="ignore",
    )

    provider: Provider = "deepgram"
    model: str = "nova-3"
    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = None
    request_timeout_s: float = Field(default=60.0, gt=0.0)
    language_hint: str | None = None
    vad_library: VADLibrary = "silero"

    deepgram_endpointing_ms: int = Field(default=300, ge=10, le=2000)
    deepgram_utterance_end_ms: int = Field(default=1000, ge=100, le=5000)

    silero_min_speech_duration_ms: int = Field(default=50, ge=10, le=500)
    silero_min_silence_duration_ms: int = Field(default=200, ge=50, le=2000)
    silero_activation_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # When True, suppress Silero ``speech_started`` emission while the persona is
    # speaking (the D-V2-X-echo-cancellation-v1-dependency no-AEC stopgap). Default
    # **False**: a hard mute also blocks a *real* barge-in onset from reaching the
    # orchestrator, so the persona could not be interrupted while speaking AND
    # (post-V8) the user's barge-in audio was withheld from the billed stream until
    # the persona finished — a transcription regression (operator-pass finding,
    # 2026-06-23). With browser/transport AEC (on by default) removing the persona's
    # echo from the inbound mic, and the orchestrator's confidence + confirm-window
    # echo rejection as the primary defense, the mute is unnecessary. Set True only
    # for a deployment proven to lack AEC (echo would otherwise fire false barge-ins).
    silero_echo_mute_while_speaking: bool = False
