"""Concrete Cartesia streaming-TTS backend (T04, D-V3-1 LOCK launch).

Implements :class:`persona_voice.tts.protocol.StreamingTTS` against the
Cartesia Sonic 3.5 WebSocket *contexts* API — the only module in
persona-voice that imports the ``cartesia`` SDK (Spec 02 adapter-boundary
discipline; callers depend on the Protocol + our
:class:`persona_voice.tts.errors.TTSError` hierarchy, never on the
vendor).

**Streaming shape (R-V3-1 + R-V3-4).** One WebSocket *context* per
utterance: text chunks are sent with ``continue_=True`` as the seam
adapter's chunker produces them (``max_buffer_delay_ms=0`` so the client
chunker is the single segmentation point per D-V3-X-chunker-placement);
audio streams back concurrently as base64 ``chunk`` events carrying raw
``pcm_s16le`` @ 24 kHz mono — the V1 outbound rail, requested natively so
no transcoding is needed (R-V3-4). The :class:`PCM16Reframer` turns the
provider's variable bursts into steady :class:`AudioChunk` frames. The
first audio frame is yielded well before the text stream completes (spec
§6 criterion #2). At end-of-text ``no_more_inputs`` flushes the context.

**Cancellation (D-V3-5 / D-V3-X-cancel-flush-additive-shape).**
:meth:`cancel` is idempotent + synchronous-effect-first: it marks the
stream cancelled and sends Cartesia's per-context cancel. The receive loop
checks the flag and stops yielding; the full six-step barge-in teardown
(transport-queue clear + watchdog) is the T09 seam adapter's job.

**Live-wire validation.** The real provider behaviour (latency, prosody,
flush/cancel timing) is validated at T14 external smoke against a real
``PERSONA_TTS_API_KEY``; the unit tests exercise this backend's structure
(fail-fast, error mapping, reframe wiring, cancel) against an injected
fake client.
"""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any, cast

from cartesia import (
    AsyncCartesia,
    CartesiaError,
)
from cartesia import (
    AuthenticationError as _CartesiaAuthError,
)
from cartesia import (
    RateLimitError as _CartesiaRateLimitError,
)
from persona.logging import get_logger

from persona_voice.tts.audio import OUTBOUND_SAMPLE_RATE, PCM16Reframer
from persona_voice.tts.catalogue import normalize_gender
from persona_voice.tts.errors import (
    TTSAuthenticationError,
    TTSError,
    TTSRateLimitError,
    TTSStreamFailureError,
)
from persona_voice.tts.types import VoiceCatalogueEntry

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator

    from persona_voice.loop.streaming import AudioChunk
    from persona_voice.model.expressivity import VoiceExpressivityChannel
    from persona_voice.tts.config import StreamingTTSConfig
    from persona_voice.tts.types import ResolvedVoice, VoiceGender

__all__ = ["CartesiaStreamingTTS"]

_PROVIDER_NAME = "cartesia"
_logger = get_logger("tts.cartesia")

# The universal fail-soft language for the synthesis fallback (Spec 32 B4): when
# the persona's voice can't speak the declared language, re-synthesise in English
# rather than crash the call.
_FALLBACK_LANGUAGE = "en"

# Research R-V3-1: Cartesia bills ~1 credit/char; effective ≈ $0.023/min on
# the Startup tier (≈ 2.3 cents/min). D-V3-X-cost: assume worst-case 2×
# overage until published rates are confirmed; the real per-session cost is
# measured at T14 + rolled into VoiceLog (T11). This seeds
# ``tts_provider_cost_cents_per_minute``.
_EST_COST_CENTS_PER_MINUTE = 2.3

#: How long a fetched voice catalogue is reused before re-walking the provider.
#: Voices change rarely; an hour keeps ``GET /v1/voices`` instant after the
#: first (~30s) walk while staying fresh enough for catalogue additions.
_CATALOGUE_CACHE_TTL_S = 3600.0


class CartesiaStreamingTTS:
    """Streaming-TTS backend for Cartesia Sonic 3.5 (D-V3-1 launch).

    Args:
        config: Streaming-TTS config (``PERSONA_TTS_*``). ``api_key`` is
            required — a missing/empty key fails fast at construction with
            :class:`TTSAuthenticationError` (Spec 02 D-02-10).
        client: Test seam — an injected ``AsyncCartesia`` (or compatible
            fake). Production passes ``None`` and the real client is built
            from ``config``. The SDK is imported only in this module.
    """

    def __init__(
        self,
        config: StreamingTTSConfig,
        *,
        client: AsyncCartesia | None = None,
        expressivity_channel: VoiceExpressivityChannel | None = None,
    ) -> None:
        key = config.api_key.get_secret_value() if config.api_key is not None else ""
        if not key:
            raise TTSAuthenticationError(
                "PERSONA_TTS_API_KEY is required for the Cartesia backend",
                context={"provider": _PROVIDER_NAME},
            )
        self._config = config
        self._model = config.model
        # V12 T3 (V12-D-1/D-4): the per-utterance expressivity hand-off. When wired,
        # the persona's stance (published by the reply producer) is read here and passed
        # as Cartesia ``generation_config`` on the utterance's sends (out-of-band, never
        # in the transcript). ``None`` ⇒ today's flat synthesis, byte-identical.
        self._expressivity_channel = expressivity_channel
        # V12 (V12-D-6): the emotion-Beta kill-switch. ``False`` drops the Beta ``emotion``
        # field while keeping the stable ``speed``/``volume`` base — the operator's
        # no-deploy mid-tier degradation.
        self._emotion_enabled = config.emotion_enabled
        # Per-call synthesis language (Spec 32 B4) — passed into ``context`` so
        # the persona's declared language is spoken with the right phonetics.
        self._language = config.language
        self._client: AsyncCartesia = client or AsyncCartesia(
            api_key=key,
            base_url=config.base_url,
            # Disable hidden SDK retries on the real-time path — N×retries
            # silently inflate worst-case latency (D-20-10 spirit).
            max_retries=0,
        )
        self._cancelled = False
        self._closed = False
        self._active_ctx: Any | None = None
        # Voice-catalogue cache (D-V6-E4 perf): ``voices.list`` walks the FULL
        # provider catalogue (~760 voices, ~30s) on every call, and the
        # client-side language filter can't short-circuit on ``limit`` for a
        # rare language — so an uncached ``GET /v1/voices`` could exceed a 25s
        # caller timeout. Walk once, cache the full (unfiltered) list, and serve
        # filtered views from memory until the TTL lapses.
        self._catalogue_cache: tuple[VoiceCatalogueEntry, ...] | None = None
        self._catalogue_cache_at: float = 0.0

    # ----- introspection / capability ---------------------------------

    @property
    def provider_name(self) -> str:
        return _PROVIDER_NAME

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def consumes_raw_text(self) -> bool:
        # D-V3-2 / D-V3-X-chunker-placement: the client chunker is
        # load-bearing; Cartesia's server buffer is zeroed
        # (max_buffer_delay_ms=0) so chunking happens exactly once.
        return False

    @property
    def cost_cents_per_minute(self) -> float:
        """Estimated provider cost for VoiceLog (T11 / D-V3-X-cost)."""
        return _EST_COST_CENTS_PER_MINUTE

    # ----- catalogue (VoiceCatalogue Protocol, D-V3-3) -----------------

    async def list_voices(
        self,
        *,
        gender: VoiceGender | None = None,
        language: str | None = None,
        limit: int | None = None,
    ) -> tuple[VoiceCatalogueEntry, ...]:
        """List Cartesia catalogue voices as data-only records (D-V3-3).

        Gender/language filters are applied client-side over the normalised
        metadata so the surface stays provider-independent; ``preview_url``
        is the provider sample URL passed through for V6/F5 (not rendered).

        Served from the cached full catalogue (walked once, ~30s) so a filtered
        query — including a rare language that would otherwise force a full walk
        every call — returns instantly after the first fill.
        """
        catalogue = await self._cached_catalogue()
        entries = [
            entry
            for entry in catalogue
            if (gender is None or entry.gender == gender)
            and (language is None or entry.language == language)
        ]
        if limit is not None:
            entries = entries[:limit]
        return tuple(entries)

    async def _cached_catalogue(self) -> tuple[VoiceCatalogueEntry, ...]:
        """Return the full provider catalogue, walking + caching it on a miss."""
        cache = self._catalogue_cache
        if cache is not None:
            age = time.monotonic() - self._catalogue_cache_at
            if age < _CATALOGUE_CACHE_TTL_S:
                return cache
        entries: list[VoiceCatalogueEntry] = []
        try:
            async for voice in self._client.voices.list(expand=["preview_file_url"]):
                entries.append(
                    VoiceCatalogueEntry(
                        voice_id=voice.id,
                        name=voice.name,
                        gender=normalize_gender(voice.gender),
                        language=voice.language,
                        description=voice.description,
                        preview_url=voice.preview_file_url,
                    )
                )
        except CartesiaError as exc:
            raise self._map_error(exc) from exc
        catalogue = tuple(entries)
        self._catalogue_cache = catalogue
        self._catalogue_cache_at = time.monotonic()
        return catalogue

    # ----- synthesis ---------------------------------------------------

    async def synthesize(
        self,
        text_stream: AsyncIterator[str],
        voice: ResolvedVoice,
    ) -> AsyncIterator[AudioChunk]:
        """Stream synthesised audio for an incremental reply-text stream.

        Spec 32 B4 fail-soft: a Cartesia voice supports a *fixed set* of
        languages, so a per-persona declared language the chosen voice cannot
        speak is rejected with ``language_not_supported``. Rather than crash the
        turn, fall back to the voice's default language so the persona still
        speaks (D-32-4). English (``self._language is None``) always streams
        directly; a declared non-English language materialises the reply first so
        the fall-back can re-synthesise the same text without losing it.
        """
        # V12-D-4: clear the expressivity slot at the START of the utterance, before any
        # token is pulled, so a prior utterance's stance cannot bleed into this one. The
        # producer republishes (if it emits a tag) while this utterance's tokens stream.
        if self._expressivity_channel is not None:
            self._expressivity_channel.reset()
        if self._language is None:
            async for chunk in self._stream_once(text_stream, voice, None):
                yield chunk
            return

        reply = [token async for token in text_stream]

        async def _replay() -> AsyncIterator[str]:
            for token in reply:
                yield token

        produced = False
        try:
            async for chunk in self._stream_once(_replay(), voice, self._language):
                produced = True
                yield chunk
            return
        except TTSStreamFailureError as exc:
            if produced or exc.context.get("error_code") != "language_not_supported":
                raise
            _logger.warning(
                "cartesia voice does not support language={lang}; retrying in English "
                "so the persona still speaks (voice={voice})",
                lang=self._language,
                voice=voice.voice_ref,
            )
        # Fail-soft (D-32-4): re-synthesise in English — the universal default.
        # If the voice cannot speak the reply even in English, degrade to no audio
        # for this turn rather than crash the call (the author should pick a voice
        # that supports the persona's language — the voice picker now filters for
        # exactly that).
        try:
            async for chunk in self._stream_once(_replay(), voice, _FALLBACK_LANGUAGE):
                yield chunk
        except TTSStreamFailureError as exc:
            if exc.context.get("error_code") != "language_not_supported":
                raise
            _logger.warning(
                "cartesia voice cannot speak this reply (voice={voice}); no audio this "
                "turn — choose a voice that supports the persona's language",
                voice=voice.voice_ref,
            )

    async def _stream_once(
        self,
        text_stream: AsyncIterator[str],
        voice: ResolvedVoice,
        language: str | None,
    ) -> AsyncIterator[AudioChunk]:
        """One context's worth of synthesis at the given language (or default)."""
        if voice.provider != _PROVIDER_NAME:
            raise TTSError(
                "resolved voice is not addressed to the cartesia provider",
                context={"provider": _PROVIDER_NAME, "voice_provider": voice.provider},
            )

        import asyncio

        self._cancelled = False
        reframer = PCM16Reframer()
        voice_param = cast("dict[str, Any]", {"mode": "id", "id": voice.voice_ref})
        output_format = {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": OUTBOUND_SAMPLE_RATE,
        }

        # One WebSocket *context* per utterance (R-V3-5 per-utterance stream
        # lifetime). ``websocket_connect()`` returns the rich connection
        # manager; ``__aenter__`` opens the live connection (the documented
        # ``.enter()`` direct-use path).
        manager = self._client.tts.websocket_connect()
        try:
            connection = await manager.__aenter__()
        except CartesiaError as exc:
            raise self._map_error(exc) from exc
        except Exception as exc:  # noqa: BLE001 — non-SDK connect failures
            # A quota/credits rejection (HTTP 402 "quota_exceeded") and other
            # handshake failures surface as a raw websockets error, NOT a
            # CartesiaError — so they bypass _map_error and would leak out as a
            # raw provider exception. Callers depend on the TTSError hierarchy
            # (never raw provider types), so map it to a domain stream failure.
            raise TTSStreamFailureError(
                "failed to open the Cartesia TTS stream — check the provider "
                "credits/quota and connectivity",
                context={"provider": _PROVIDER_NAME, "error": repr(exc)[:200]},
            ) from exc

        # Include ``language`` only when given (Spec 32 B4) — preserving the
        # provider-default behaviour for an unset (English-default / fallback) call.
        language_kwarg: dict[str, Any] = {"language": language} if language is not None else {}
        ctx = connection.context(
            model_id=self._model,
            voice=cast("Any", voice_param),
            output_format=cast("Any", output_format),
            max_buffer_delay_ms=self._config.cartesia_max_buffer_delay_ms,
            **cast("Any", language_kwarg),
        )
        self._active_ctx = ctx
        sender: asyncio.Task[None] = asyncio.create_task(
            self._drain_text(ctx, text_stream, voice_param)
        )

        try:
            async for event in ctx.receive():
                if self._cancelled:
                    break
                event_type = getattr(event, "type", None)
                if event_type == "chunk":
                    # ``audio`` exists only on the Chunk variant of the
                    # response union — getattr narrows without importing the
                    # vendor's internal model types.
                    audio = getattr(event, "audio", None)
                    if audio:
                        for frame in reframer.push(audio):
                            yield frame
                elif event_type == "done":
                    break
                elif event_type == "error":
                    raise TTSStreamFailureError(
                        getattr(event, "message", "cartesia stream error"),
                        context={
                            "provider": _PROVIDER_NAME,
                            "error_code": str(getattr(event, "error_code", "")),
                            "status_code": str(getattr(event, "status_code", "")),
                        },
                    )
            if not self._cancelled:
                tail = reframer.flush()
                if tail is not None:
                    yield tail
        except CartesiaError as exc:
            raise self._map_error(exc) from exc
        finally:
            await self._cancel_task(sender)
            self._active_ctx = None
            with contextlib.suppress(CartesiaError):
                await connection.close()

    async def _drain_text(
        self,
        ctx: Any,  # noqa: ANN401 — vendor AsyncWebSocketContext (dynamic SDK boundary)
        text_stream: AsyncIterator[str],
        voice_param: dict[str, Any],
    ) -> None:
        """Feed reply-text chunks into the context, then signal end-of-input.

        Sender side of the concurrent send/receive pair. Provider errors
        here surface to the consumer through the receive loop's error event
        / connection close; this task swallows them so cancellation does
        not raise out of the background task.

        V12-D-1/D-4: the persona's stance is applied here as Cartesia
        ``generation_config``. It is resolved ONCE, on the first chunk — the
        producer publishes the stance while producing that first chunk's text (it
        leads with the tag, which is stripped before any spoken char), so the slot
        is set by the time the chunk arrives — and reused for every send in this
        context (Cartesia's "same value per context" guidance, per-context
        consistency). ``None`` (no tag / no channel) ⇒ no ``generation_config`` sent
        ⇒ today's flat, byte-identical call.
        """
        generation_config: dict[str, float | str] | None = None
        resolved = False
        try:
            async for chunk in text_stream:
                if self._cancelled:
                    return
                if not resolved:
                    resolved = True
                    generation_config = self._pending_generation_config()
                if chunk:
                    await self._send_chunk(ctx, chunk, voice_param, generation_config)
            if not self._cancelled:
                await ctx.no_more_inputs()
        except CartesiaError:
            return

    def _pending_generation_config(self) -> dict[str, float | str] | None:
        """Read this utterance's expressivity as a ``generation_config`` dict, or ``None``.

        Fail-safe (V12-D-5): ANY error reading/resolving the channel degrades to ``None``
        (a flat read) — expressivity never breaks or interrupts synthesis. Because the
        controls are out-of-band, a failure here cannot corrupt the spoken transcript.
        """
        channel = self._expressivity_channel
        if channel is None:
            return None
        try:
            expr = channel.take()
            if expr is None:
                return None
            # include_emotion honours the V12-D-6 kill-switch: off ⇒ speed/volume-only.
            config = expr.to_generation_config(include_emotion=self._emotion_enabled)
            return config or None
        except Exception:  # noqa: BLE001 — expressivity is additive; never break the call
            _logger.warning("voice expressivity resolve failed; synthesising flat")
            return None

    async def _send_chunk(
        self,
        ctx: Any,  # noqa: ANN401 — vendor AsyncWebSocketContext (dynamic SDK boundary)
        chunk: str,
        voice_param: dict[str, Any],
        generation_config: dict[str, float | str] | None,
    ) -> None:
        """Send one text chunk, attaching ``generation_config`` only when present.

        Omitting the kwarg entirely (the ``None`` path) keeps the call byte-identical to
        pre-V12 — the criterion that the no-tag / no-channel path is unchanged.
        """
        if generation_config is None:
            await ctx.send(transcript=chunk, voice=cast("Any", voice_param), continue_=True)
        else:
            await ctx.send(
                transcript=chunk,
                voice=cast("Any", voice_param),
                continue_=True,
                generation_config=cast("Any", generation_config),
            )

    async def cancel(self) -> None:
        """Abort in-flight synthesis (V4 barge-in). Idempotent."""
        if self._cancelled:
            return
        self._cancelled = True
        ctx = self._active_ctx
        if ctx is not None:
            with contextlib.suppress(CartesiaError):
                await ctx.cancel()

    async def close(self) -> None:
        """Close the provider connection gracefully. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(CartesiaError):
            await self._client.close()

    # ----- helpers -----------------------------------------------------

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None]) -> None:
        import asyncio

        if task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, CartesiaError):
            await task

    @staticmethod
    def _map_error(exc: CartesiaError) -> TTSError:
        """Map a Cartesia SDK exception to the TTS domain hierarchy."""
        context = {"provider": _PROVIDER_NAME, "error_class": type(exc).__name__}
        if isinstance(exc, _CartesiaAuthError):
            return TTSAuthenticationError(str(exc), context=context)
        if isinstance(exc, _CartesiaRateLimitError):
            return TTSRateLimitError(str(exc), context=context)
        # Connection drops, timeouts, 5xx, and any other provider failure
        # during streaming surface as a recoverable stream failure — the
        # persona falls silent cleanly (spec §6 criterion #11).
        return TTSStreamFailureError(str(exc), context=context)
