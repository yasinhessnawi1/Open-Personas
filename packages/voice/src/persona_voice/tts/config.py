"""Streaming-TTS configuration loaded from ``PERSONA_TTS_*`` env vars.

:class:`StreamingTTSConfig` is the input to
:func:`persona_voice.tts._factory.load_streaming_tts`. Mirrors the Spec 02
:class:`persona.backends.config.BackendConfig` / V2
:class:`persona_voice.stt.config.StreamingSTTConfig` shape verbatim:
:class:`~pydantic_settings.BaseSettings` with ``env_prefix="PERSONA_TTS_"``
+ ``extra="ignore"``; :class:`~pydantic.SecretStr` ``api_key`` so
``repr(config)`` never leaks it; ``Field`` constraints on every numeric
knob so misconfigured operators fail fast at construction (the Spec 02
D-02-10 + V3 D-V3-X-cost precedent).

**Provider Literal.** ``cartesia`` is the D-V3-1 LOCK launch provider
(Sonic 3.5). ``elevenlabs`` (Spec V14, D-V14-9) is the utterance-level
text-language-auto-follow provider: one voice identity speaks the language
the reply TEXT is written in, per reply, with NO language told to the API
(the T0 POC proved same-voice naturalness across en/no/ar/es/fr/vi at
flash-class latency). Its backend slots behind the SAME
:class:`persona_voice.tts.protocol.StreamingTTS` seam and is selected by
``PERSONA_TTS_PROVIDER=elevenlabs`` (rollback = flip the selector back, the
ElevenLabs knobs park). The Literal pins both.

**Chunking knobs (D-V3-2).** The hybrid sentence-level + first-chunk-shorter
parameters the T05 chunker reads. Defaults are the locked D-V3-2 values
(multi-project convergent numbers from R-V3-2 + R-V3-5): first chunk emits
at a clause delimiter once ``chunk_min_first_chars`` is buffered, force-emits
at ``chunk_max_first_words``; subsequent chunks emit at sentence enders with
``chunk_min_chars`` floor and ``chunk_max_chars`` hard split.

**Cartesia knobs.** ``cartesia_version`` pins the dated API contract
(D-V3-1 — ``Cartesia-Version: 2026-03-01``); ``cartesia_max_buffer_delay_ms``
is the server-side text buffer, defaulted to ``0`` because the client
chunker is load-bearing (D-V3-X-chunker-placement composition rule:
chunking happens exactly once, on exactly one side).
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Provider", "StreamingTTSConfig"]

Provider = Literal[
    "cartesia",
    "elevenlabs",
]


class StreamingTTSConfig(BaseSettings):
    """Env-driven configuration for a single :class:`StreamingTTS` backend.

    Reads from ``PERSONA_TTS_*`` env vars. Mirrors
    :class:`persona.backends.config.BackendConfig` /
    :class:`persona_voice.stt.config.StreamingSTTConfig` per the Spec 02
    mirror discipline.

    Attributes:
        provider: Which streaming-TTS backend to load. ``cartesia`` is the
            D-V3-1 LOCK launch.
        model: Model identifier within the provider. ``sonic-3.5`` is the
            Cartesia launch model per R-V3-1.
        api_key: Provider API key. Stored as
            :class:`~pydantic.SecretStr` so ``repr(config)`` does not leak
            it. Construction-time validation in the concrete backend fails
            fast with
            :class:`persona_voice.tts.errors.TTSAuthenticationError` when
            missing (D-02-10 fail-fast precedent).
        base_url: Optional override for the provider's default streaming
            endpoint (proxies, self-hosted endpoints, providers not in the
            launch set).
        request_timeout_s: HTTP / WebSocket request timeout in seconds.
            Default 60.0 mirrors Spec 02 ``BackendConfig.request_timeout_s``.
        voice_default: Catalogue voice id used when a persona has no
            explicit ``voice`` (D-V3-4). ``None`` means no fallback is
            configured — resolution of a voice-less persona then fails
            with
            :class:`persona_voice.tts.errors.TTSVoiceNotFoundError`.
        chunk_min_first_chars: Minimum buffered chars before the first
            chunk may emit at a clause delimiter (D-V3-2). Default 10.
        chunk_max_first_words: Force-emit the first chunk after this many
            words if no clause/sentence delimiter has appeared (D-V3-2).
            Default 30.
        chunk_min_chars: Minimum length of a subsequent (non-first) chunk;
            shorter sentences are merged forward (D-V3-2). Default 20.
        chunk_max_chars: Hard split at the nearest whitespace once a chunk
            reaches this length — also bounds V4 barge-in stop resolution
            (D-V3-2). Default 300.
        cartesia_version: Pinned ``Cartesia-Version`` API-contract date
            (D-V3-1). Default ``"2026-03-01"``.
        cartesia_max_buffer_delay_ms: Cartesia server-side text buffer in
            ms (0–5000). Default 0 — the client chunker is load-bearing
            (D-V3-X-chunker-placement); a non-zero value would double-buffer
            against the chunker.
        emotion_enabled: V12 emotion-Beta kill-switch (V12-D-6). ``True``
            (default) sends Cartesia's Beta ``emotion`` control alongside the
            stable ``speed``/``volume``; ``False`` drops ``emotion`` and keeps
            speed/volume — an operator's no-deploy mid-tier degradation if the
            Beta layer misbehaves in production.
        elevenlabs_api_key: ElevenLabs API key (Spec V14). Reads the
            spec-named, un-prefixed ``PERSONA_ELEVENLABS_API_KEY`` (NOT
            ``PERSONA_TTS_*``) via a ``validation_alias`` so it matches the var
            the owner provisions. :class:`~pydantic.SecretStr` — never leaks in
            ``repr``. The concrete ElevenLabs backend fails fast at
            construction when this is missing while ``provider="elevenlabs"``.
        elevenlabs_model: ElevenLabs model id (default ``eleven_flash_v2_5`` —
            the flash-class, ~75ms, 32-language model the T0 POC measured at
            158ms warm first-audio). Reads ``PERSONA_TTS_ELEVENLABS_MODEL``.
        elevenlabs_voice_default: ElevenLabs catalogue voice id used as the
            provider-scoped fallback for a voice-less persona under
            ``provider="elevenlabs"`` (the D-V3-4 default, parallel to
            ``voice_default`` which stays the Cartesia default). Reads
            ``PERSONA_TTS_ELEVENLABS_VOICE_DEFAULT``. The composition wiring
            that selects the provider-appropriate default is T4's concern.
    """

    model_config = SettingsConfigDict(
        env_prefix="PERSONA_TTS_",
        extra="ignore",
        # Let the aliased ``elevenlabs_api_key`` (which reads the spec-named,
        # un-prefixed ``PERSONA_ELEVENLABS_API_KEY`` — D-V14-9) still be
        # constructible by its Python name, e.g. in tests. A no-op for every
        # non-aliased field.
        populate_by_name=True,
    )

    provider: Provider = "cartesia"
    model: str = "sonic-3.5"
    api_key: SecretStr | None = Field(default=None, repr=False)
    base_url: str | None = None
    request_timeout_s: float = Field(default=60.0, gt=0.0)
    voice_default: str | None = None
    # The per-call synthesis language code (Spec 32 B4). ``None`` ⇒ no language
    # param (provider default). build_agent_session pins it to the persona's
    # declared language via the capability registry; Cartesia voices are
    # multilingual, so this is a language code, not a voice constraint.
    language: str | None = None

    chunk_min_first_chars: int = Field(default=10, ge=1, le=200)
    chunk_max_first_words: int = Field(default=30, ge=1, le=200)
    chunk_min_chars: int = Field(default=20, ge=1, le=500)
    chunk_max_chars: int = Field(default=300, ge=20, le=2000)

    # V12 (V12-D-6): the emotion-Beta operator kill-switch. Cartesia's ``emotion``
    # control is Beta while ``speed``/``volume`` are stable; if the Beta layer
    # destabilises in production, an operator sets ``PERSONA_TTS_EMOTION_ENABLED=false``
    # to drop the emotion field and keep the stable speed/volume base — graceful
    # mid-tier degradation with NO code deploy. Default ``True`` (emotion active).
    emotion_enabled: bool = True

    cartesia_version: str = "2026-03-01"
    cartesia_max_buffer_delay_ms: int = Field(default=0, ge=0, le=5000)

    # --- ElevenLabs (Spec V14 D-V14-9) — provider-scoped knobs, parked unless
    # ``provider="elevenlabs"``. ``elevenlabs_api_key`` escapes the
    # ``PERSONA_TTS_`` prefix to read the spec-named ``PERSONA_ELEVENLABS_API_KEY``
    # the owner provisions (the same var the T0 POC used); ``elevenlabs_model``
    # + ``elevenlabs_voice_default`` ride the prefix like the ``cartesia_*``
    # knobs. SecretStr so ``repr`` never leaks the key.
    elevenlabs_api_key: SecretStr | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("PERSONA_ELEVENLABS_API_KEY"),
    )
    elevenlabs_model: str = "eleven_flash_v2_5"
    elevenlabs_voice_default: str | None = None
