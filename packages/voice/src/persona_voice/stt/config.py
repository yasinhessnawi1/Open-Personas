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
``gladia`` (Spec V14 D-V14-9) is the architecture-level code-switching
provider (Solaria — ~100 languages, no language list required); its
streaming backend slots behind the SAME Protocol seam and is selected by
``PERSONA_STT_PROVIDER=gladia`` (rollback = flip the selector back, the
Gladia knobs park). The Literal pins all four even though only Deepgram +
Gladia ship a concrete backend — keeping the enumeration honest about
what shape the Protocol covers.

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

import os
from typing import Literal

from persona.logging import get_logger
from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Provider", "StreamingSTTConfig", "VADLibrary"]

_logger = get_logger("stt.config")

# Warn at most ONCE per process that ``PERSONA_STT_LANGUAGE_HINT`` is set — a
# deprecated pre-Spec-32 global stopgap (Spec V14 D-V14-5). Guarded by a
# module-level flag so ``model_copy`` (which re-runs validators, e.g. every
# ``apply_stt_route`` call) does not re-warn.
_LANGUAGE_HINT_DEPRECATION_WARNED = False

Provider = Literal[
    "deepgram",
    "speechmatics",
    "whisper-streaming",
    "gladia",
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
        gladia_api_key: Gladia API key (Spec V14). Reads the spec-named,
            un-prefixed ``PERSONA_GLADIA_API_KEY`` (NOT ``PERSONA_STT_*``)
            via a ``validation_alias`` so it matches the var the owner
            provisions. :class:`~pydantic.SecretStr` — never leaks in
            ``repr``. The concrete Gladia backend fails fast at construction
            with :class:`STTAuthenticationError` when this is missing while
            ``provider="gladia"`` (D-02-10 discipline).
        gladia_model: Gladia model id (default ``solaria-1`` — the
            code-switching live model). Reads ``PERSONA_STT_GLADIA_MODEL``.
    """

    model_config = SettingsConfigDict(
        env_prefix="PERSONA_STT_",
        extra="ignore",
        # Let the aliased fields (``gladia_api_key`` reads the spec-named,
        # un-prefixed ``PERSONA_GLADIA_API_KEY`` — D-V14-9) still be
        # constructible by their Python name, e.g. in tests. A no-op for every
        # non-aliased field.
        populate_by_name=True,
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

    # --- Gladia (Spec V14 D-V14-9) — provider-scoped knobs, parked unless
    # ``provider="gladia"``. ``gladia_api_key`` escapes the ``PERSONA_STT_``
    # prefix to read the spec-named ``PERSONA_GLADIA_API_KEY`` the owner
    # provisions (the same var the T0 POC used); ``gladia_model`` rides the
    # prefix like the ``deepgram_*`` knobs. Kept as :class:`~pydantic.SecretStr`
    # so ``repr(config)`` never leaks it (the ``api_key`` discipline).
    gladia_api_key: SecretStr | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("PERSONA_GLADIA_API_KEY"),
    )
    #: Gladia model id. ``solaria-1`` is the code-switching-capable live model
    #: (the ONLY model Gladia's live/streaming endpoint supports at V14 T0);
    #: the T0 POC proved it survives mid-utterance language switches without a
    #: language list (D-V14-1). Reads ``PERSONA_STT_GLADIA_MODEL``.
    gladia_model: str = "solaria-1"

    @model_validator(mode="after")
    def _warn_deprecated_language_hint(self) -> StreamingSTTConfig:
        """Warn once if the deprecated ``PERSONA_STT_LANGUAGE_HINT`` env is set.

        Spec V14 (D-V14-5): the global single-language hint is a pre-Spec-32
        stopgap. Live calls pin the persona's declared language per call
        (``apply_stt_route``); one-shot dictation context-pins per request; and
        an utterance-level provider (``gladia``) derives language from the audio
        and ignores it entirely. Checked against the raw env var (not the field,
        which ``apply_stt_route`` legitimately sets per call) and warned at most
        once per process via a module flag.
        """
        global _LANGUAGE_HINT_DEPRECATION_WARNED  # noqa: PLW0603 — process-once guard
        if not _LANGUAGE_HINT_DEPRECATION_WARNED and os.environ.get("PERSONA_STT_LANGUAGE_HINT"):
            _LANGUAGE_HINT_DEPRECATION_WARNED = True
            _logger.warning(
                "PERSONA_STT_LANGUAGE_HINT is set but DEPRECATED (Spec V14 D-V14-5): "
                "it is the pre-Spec-32 global single-language stopgap. Live calls pin "
                "the persona's declared language per call, one-shot dictation "
                "context-pins per request, and utterance-level providers (gladia) "
                "ignore it. Unset it to avoid forcing every clip through one language."
            )
        return self

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
