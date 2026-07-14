"""Per-call language plan, resolved once at session build (Spec 32 B2).

The composition root resolves a persona's ``identity.language_default`` into a
single :class:`CallLanguagePlan` and threads it through the call: the STT route
pins the Deepgram model + code (B3), the TTS route pins the Cartesia code (B4),
and :attr:`CallLanguagePlan.reply_language` — the language TTS will *actually*
speak — drives the prompt builder's "respond in {language}" injection (B5).

Keying the reply language on the TTS route (not the raw declared language) is the
correctness point: if the providers cannot speak the declared language and TTS
falls back to English, the reply text falls back with it, so the user never hears
English phonetics over Norwegian words. One resolution, one source of truth.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.language_capability import (
    CanonicalLanguage,
    CapabilityRegistry,
    LanguageFallbackEvent,
    STTRoute,
    TTSRoute,
    default_capability_registry,
)
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from persona_voice.stt.config import StreamingSTTConfig
    from persona_voice.tts.config import StreamingTTSConfig

__all__ = [
    "CallLanguagePlan",
    "apply_stt_route",
    "apply_tts_route",
    "maybe_apply_stt_route",
    "maybe_apply_tts_route",
    "resolve_call_languages",
    "stt_is_utterance_level",
    "tts_is_utterance_level",
]

# Spec V14 (D-V14-5): providers whose STT/TTS operate at the UTTERANCE level —
# language derived from the utterance itself (Gladia code-switching STT;
# ElevenLabs text-auto-follow TTS) — must NOT receive the per-persona language
# route. ``apply_stt_route`` / ``apply_tts_route`` are the incumbent-only pinning
# that would re-narrow such a provider to a single declared language, defeating
# the whole spec (D-V14-1). The route functions stay OPERATIONAL for the
# incumbent providers (kept for the fallback path — D-V14-5 "leaves the hot path,
# kept operational"). Rollback = flip the provider selector back to the
# incumbent, and the route applies again with no code change.
_UTTERANCE_LEVEL_STT: frozenset[str] = frozenset({"gladia"})
_UTTERANCE_LEVEL_TTS: frozenset[str] = frozenset({"elevenlabs"})


def stt_is_utterance_level(provider: str) -> bool:
    """Whether the STT provider derives language from the utterance itself.

    ``True`` ⇒ the composition root SKIPS :func:`apply_stt_route` (the provider
    code-switches across languages with no language list — Gladia, D-V14-1);
    ``False`` ⇒ the incumbent path pins the persona's declared language (Spec 32).
    """
    return provider in _UTTERANCE_LEVEL_STT


def tts_is_utterance_level(provider: str) -> bool:
    """Whether the TTS provider auto-follows the reply TEXT's language.

    ``True`` ⇒ the composition root SKIPS :func:`apply_tts_route` AND selects the
    B5 reply-language MIRROR directive (the persona replies in the user's
    language, defaulting to its own — ElevenLabs, D-V14-1/D-V14-12); ``False`` ⇒
    the incumbent path pins the synthesis language (Spec 32).
    """
    return provider in _UTTERANCE_LEVEL_TTS


class CallLanguagePlan(BaseModel):
    """The resolved language routing for one voice call (frozen)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stt: STTRoute
    tts: TTSRoute

    @property
    def reply_language(self) -> CanonicalLanguage:
        """The language the LLM must reply in — what TTS will actually speak."""
        return self.tts.canonical

    @property
    def fallbacks(self) -> tuple[LanguageFallbackEvent, ...]:
        """Every fail-soft that fired (STT and/or TTS), for logging + the event."""
        return tuple(e for e in (self.stt.fallback, self.tts.fallback) if e is not None)


def resolve_call_languages(
    language_default: str,
    *,
    registry: CapabilityRegistry | None = None,
) -> CallLanguagePlan:
    """Resolve a persona's declared language into the per-call routing plan.

    Args:
        language_default: the persona's ``identity.language_default``.
        registry: the capability registry (defaults to the built-in v1 matrices).

    Returns:
        The :class:`CallLanguagePlan` — STT route, TTS route, and the derived
        reply language. Never raises for an unsupported language: it fails soft to
        English and records the fallback events on the routes.
    """
    reg = registry if registry is not None else default_capability_registry()
    return CallLanguagePlan(
        stt=reg.resolve_stt(language_default),
        tts=reg.resolve_tts(language_default),
    )


def maybe_apply_stt_route(config: StreamingSTTConfig, route: STTRoute) -> StreamingSTTConfig:
    """Apply the STT route UNLESS the provider is utterance-level (Spec V14 D-V14-5).

    The composition-root entry point: an incumbent STT provider (Deepgram) gets
    the per-persona language pinned (:func:`apply_stt_route`, byte-identical to
    Spec 32); an utterance-level provider (Gladia) is returned UNTOUCHED — it
    code-switches from the audio itself and pinning would defeat D-V14-1. Rollback
    = flip the provider back to the incumbent and the route applies again.
    """
    if stt_is_utterance_level(config.provider):
        return config
    return apply_stt_route(config, route)


def maybe_apply_tts_route(config: StreamingTTSConfig, route: TTSRoute) -> StreamingTTSConfig:
    """Apply the TTS route UNLESS the provider is utterance-level (Spec V14 D-V14-5).

    Incumbent (Cartesia) gets the synthesis language pinned (:func:`apply_tts_route`,
    byte-identical to Spec 32); an utterance-level provider (ElevenLabs) is returned
    UNTOUCHED — it speaks the language the reply TEXT is written in (D-V14-1).
    """
    if tts_is_utterance_level(config.provider):
        return config
    return apply_tts_route(config, route)


def apply_stt_route(config: StreamingSTTConfig, route: STTRoute) -> StreamingSTTConfig:
    """Pin a Deepgram config to a resolved STT route for this call (Spec 32 B3).

    Overrides the model + language code per the persona's declared language,
    replacing the global ``PERSONA_STT_LANGUAGE_HINT`` default — nova-3 + ``no``
    for Norwegian (D-32-X-deepgram-no-nova3). The Deepgram backend already reads
    ``config.model`` + ``config.language_hint``, so pinning the config before the
    socket opens is the whole change. Returns a copy; the base config is untouched.
    """
    return config.model_copy(update={"model": route.model, "language_hint": route.code})


def apply_tts_route(config: StreamingTTSConfig, route: TTSRoute) -> StreamingTTSConfig:
    """Pin a Cartesia config to a resolved TTS route for this call (Spec 32 B4).

    Sets the synthesis ``language`` code per the persona's declared language —
    the missing parameter that made Norwegian text read with English phonetics.
    Cartesia voices are multilingual, so this is purely a language code, not a
    voice constraint (D-32-4). Returns a copy; the base config is untouched.
    """
    return config.model_copy(update={"language": route.code})
