"""ElevenLabs concrete :class:`StreamingTTS` backend (Spec V14 D-V14-9).

The utterance-level, text-language-auto-follow TTS provider: ONE voice identity
speaks the language the reply TEXT is written in — per reply, with **no
language code ever sent to the API** (D-V14-1). The V14 T0 POC proved the same
``voice_id`` renders en/no/ar/es/fr/vi naturally at flash-class latency (158ms
warm first-audio p50), which Cartesia's per-call language pinning could not do.

**Slots behind the SAME V3 seam.** A SIBLING of
:class:`persona_voice.tts.cartesia_backend.CartesiaStreamingTTS` behind the
:class:`persona_voice.tts.protocol.StreamingTTS` Protocol, selected by
``PERSONA_TTS_PROVIDER=elevenlabs`` (D-V14-3 kill-switch: flip back to
``cartesia`` and this parks). Also conforms to
:class:`persona_voice.tts.catalogue.VoiceCatalogue` so the voice-selector +
auto-pick see ElevenLabs voices (D-V14-4).

**No SDK — raw transport (D-V14-8).** Speaks ElevenLabs' WebSocket +
``/v1/voices`` REST directly over ``websockets`` + ``httpx`` (both already in
the tree). Both transports are injectable (``ws_connect`` / ``http_get_voices``
constructor seams) so CI exercises the full message-handling logic against
scripted fakes with zero live spend.

**Synthesis lifecycle (D-V14-10 — per-utterance socket).** One
``stream-input`` WebSocket PER utterance (the incumbent Cartesia per-context
lifetime; T0 measured this well within budget, so the multi-context variant is
a reserved falsification upgrade, not v1). Query params pin
``model_id=<elevenlabs_model>``, ``output_format=pcm_24000`` (the V1 outbound
rail — 24 kHz mono PCM16, zero transcode; T0 proved PCM works on the free tier),
and ``auto_mode=true`` (defer chunking to our client chunker — D-V3-2, so
``consumes_raw_text`` is ``False``). The wire protocol: an init ``{"text": " "}``,
then ``{"text": "<chunk> "}`` per chunk, then ``{"text": ""}`` to end; responses
carry base64 ``audio`` + a terminal ``{"isFinal": true}``. **``language_code`` is
NEVER sent** — the model detects the language from the text, which is the entire
point of the spec.

**Cancellation (D-V3-5).** :meth:`cancel` is idempotent + synchronous-effect-
first: it marks the stream cancelled and closes the active socket, which ends
the receive loop; :meth:`synthesize`'s iterator stops yielding. The full
barge-in teardown (transport-queue clear + watchdog) is the T09 seam adapter's
job.

**Expressivity (D-V14-16).** The V12 expressivity channel is ACCEPTED (factory
symmetry with Cartesia) and IGNORED — ElevenLabs synthesis is flat at v1 (the
V12-D-5 graceful-degrade posture). Mapping stance → ``voice_settings`` is future
work, out of V14 scope.

**Tier gate (D-V14-11).** A provider rejection of ``pcm_24000`` (or any
handshake/error frame) maps to a clean :class:`TTSError` — NEVER a silent MP3
fallback (a hidden decoder on the hot path is worse than an honest error).

**Error mapping.** Provider/transport exceptions are caught at the adapter
boundary and re-raised through the :class:`TTSError` hierarchy: an auth/tier
handshake rejection → :class:`TTSAuthenticationError`, anything else →
:class:`TTSStreamFailureError` (the persona falls silent cleanly, spec §6 #11).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote, urlencode

from persona.logging import get_logger

from persona_voice.tts.audio import OUTBOUND_SAMPLE_RATE, PCM16Reframer
from persona_voice.tts.catalogue import normalize_gender
from persona_voice.tts.errors import (
    TTSAuthenticationError,
    TTSError,
    TTSStreamFailureError,
)
from persona_voice.tts.types import VoiceCatalogueEntry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from types import TracebackType

    from persona_voice.loop.streaming import AudioChunk
    from persona_voice.model.expressivity import VoiceExpressivityChannel
    from persona_voice.tts.config import StreamingTTSConfig
    from persona_voice.tts.types import ResolvedVoice, VoiceGender

__all__ = ["ElevenLabsStreamingTTS"]

_PROVIDER_NAME = "elevenlabs"
_logger = get_logger("tts.elevenlabs")

_WS_BASE = "wss://api.elevenlabs.io/v1/text-to-speech"
_VOICES_URL = "https://api.elevenlabs.io/v1/voices"

# The V1 rail output format (D-V14-11): 24 kHz mono PCM16, headerless — feeds
# PCM16Reframer with zero transcode. T0 proved pcm_24000 works on the free tier.
_OUTPUT_FORMAT = f"pcm_{OUTBOUND_SAMPLE_RATE}"

# Research R-V14 / T0: ElevenLabs is credit-metered (flash ≈ 0.5–1 credit/char),
# NOT natively per-minute. This is a ROUGH placeholder for VoiceLog seeding at
# ~150 wpm on the Creator tier; the real rate is inherited by the M-track
# voice-pricing spec (D-V14-7), which the T0 report flags. Do NOT treat as
# authoritative billing.
_EST_COST_CENTS_PER_MINUTE = 6.0

#: How long a fetched voice catalogue is reused before re-walking the provider
#: (mirrors the Cartesia backend's cache — voices change rarely).
_CATALOGUE_CACHE_TTL_S = 3600.0


class _WebSocketLike(Protocol):
    """Minimal async WebSocket surface the synthesis path depends on."""

    async def send(self, data: str) -> None: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def __anext__(self) -> str | bytes: ...

    async def close(self) -> None: ...


class _WebSocketCM(Protocol):
    """Async context manager yielding a :class:`_WebSocketLike`."""

    async def __aenter__(self) -> _WebSocketLike: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...


class ElevenLabsStreamingTTS:
    """Streaming-TTS backend for ElevenLabs (Spec V14 D-V14-9).

    Args:
        config: Streaming-TTS config (``PERSONA_TTS_*`` + the aliased
            ``PERSONA_ELEVENLABS_API_KEY``). A missing/empty key fails fast at
            construction with :class:`TTSAuthenticationError` (D-02-10).
        expressivity_channel: Accepted for factory symmetry; IGNORED at v1
            (D-V14-16 — flat synthesis).
        ws_connect: Test seam — ``(ws_url) -> async context manager`` yielding a
            WebSocket-like object. Production passes ``None`` (default
            ``websockets.connect`` with the ``xi-api-key`` header).
        http_get_voices: Test seam — ``() -> Awaitable[list[dict]]`` returning
            raw ``/v1/voices`` records. Production passes ``None`` (default
            ``httpx`` GET).
    """

    def __init__(
        self,
        config: StreamingTTSConfig,
        *,
        expressivity_channel: VoiceExpressivityChannel | None = None,
        ws_connect: Callable[[str], _WebSocketCM] | None = None,
        http_get_voices: Callable[[], Awaitable[list[dict[str, Any]]]] | None = None,
    ) -> None:
        key = config.elevenlabs_api_key.get_secret_value() if config.elevenlabs_api_key else ""
        if not key:
            raise TTSAuthenticationError(
                "PERSONA_ELEVENLABS_API_KEY is required for the ElevenLabs backend",
                context={"provider": _PROVIDER_NAME},
            )
        self._config = config
        self._model = config.elevenlabs_model
        # D-V14-16: the expressivity channel is accepted but never read (flat).
        self._expressivity_channel = expressivity_channel
        self._ws_connect = ws_connect or self._default_ws_connect
        self._http_get_voices = http_get_voices or self._default_get_voices
        self._cancelled = False
        self._closed = False
        self._active_ws: _WebSocketLike | None = None
        # Cache the RAW provider records (not derived entries) so a later call
        # with a different language filter re-derives dialect metadata correctly.
        self._raw_cache: list[dict[str, Any]] | None = None
        self._raw_cache_at: float = 0.0

    # ----- introspection / capability ---------------------------------

    @property
    def provider_name(self) -> str:
        return _PROVIDER_NAME

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def consumes_raw_text(self) -> bool:
        # D-V3-2: the client chunker is load-bearing; auto_mode defers to it.
        return False

    @property
    def cost_cents_per_minute(self) -> float:
        """Rough VoiceLog seed (D-V14-7 — the M-track spec sets the real rate)."""
        return _EST_COST_CENTS_PER_MINUTE

    # ----- synthesis ---------------------------------------------------

    async def synthesize(
        self,
        text_stream: AsyncIterator[str],
        voice: ResolvedVoice,
    ) -> AsyncIterator[AudioChunk]:
        """Stream synthesised audio for an incremental reply-text stream.

        One per-utterance ``stream-input`` WebSocket; NO ``language_code`` on the
        wire (the model auto-follows the reply TEXT's language — D-V14-1). Yields
        :class:`AudioChunk` frames as the base64 audio arrives, reframed to the
        V1 rail. The first frame is yielded before the text stream completes.
        """
        if voice.provider != _PROVIDER_NAME:
            raise TTSError(
                "resolved voice is not addressed to the elevenlabs provider",
                context={"provider": _PROVIDER_NAME, "voice_provider": voice.provider},
            )
        self._cancelled = False
        reframer = PCM16Reframer()
        url = self._stream_input_url(voice.voice_ref)

        try:
            manager = self._ws_connect(url)
            connection = await manager.__aenter__()
        except TTSError:
            raise
        except Exception as exc:  # noqa: BLE001 — adapter-boundary handshake mapping
            self._raise_mapped(exc)

        self._active_ws = connection
        sender: asyncio.Task[None] = asyncio.create_task(self._drain_text(connection, text_stream))
        try:
            await connection.send(json.dumps({"text": " "}))  # init frame
            async for raw in connection:
                if self._cancelled:
                    break
                frames, is_final = self._handle_message(raw, reframer)
                for frame in frames:
                    yield frame
                if is_final:
                    break
            if not self._cancelled:
                tail = reframer.flush()
                if tail is not None:
                    yield tail
        except TTSError:
            raise
        except Exception as exc:  # noqa: BLE001 — adapter-boundary mapping
            # A cancel-initiated socket close ends the receive loop; that is a
            # clean stop, not a failure.
            if self._cancelled:
                return
            self._raise_mapped(exc)
        finally:
            await self._cancel_task(sender)
            self._active_ws = None
            with contextlib.suppress(Exception):
                await manager.__aexit__(None, None, None)

    def _handle_message(
        self, raw: str | bytes, reframer: PCM16Reframer
    ) -> tuple[list[AudioChunk], bool]:
        """Parse one WS message → (audio frames, is_final). Defensive.

        An ``{"error": …}`` / ``{"message": …}`` frame raises a mapped
        :class:`TTSError` (never a silent skip). A malformed/non-JSON frame is
        ignored.
        """
        if isinstance(raw, bytes):
            return [], False
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return [], False
        if not isinstance(msg, dict):
            return [], False
        if msg.get("error") is not None:
            reason = str(msg.get("message") or msg.get("error"))
            raise TTSStreamFailureError(
                "elevenlabs stream error",
                context={"provider": _PROVIDER_NAME, "error": reason[:200]},
            )
        frames: list[AudioChunk] = []
        audio_b64 = msg.get("audio")
        if audio_b64:
            try:
                pcm = base64.b64decode(audio_b64)
            except (ValueError, TypeError):
                pcm = b""
            if pcm:
                frames = reframer.push(pcm)
        return frames, bool(msg.get("isFinal"))

    async def _drain_text(self, ws: _WebSocketLike, text_stream: AsyncIterator[str]) -> None:
        """Feed reply-text chunks into the socket, then signal end-of-input.

        Sender side of the concurrent send/receive pair. Each chunk is sent as
        ``{"text": "<chunk> "}`` (ElevenLabs requires a trailing space); a final
        ``{"text": ""}`` flushes. Provider errors here surface to the consumer
        through the receive loop; this task swallows them so cancellation does
        not raise out of the background task. NO ``language_code`` is ever sent.
        """
        try:
            async for chunk in text_stream:
                if self._cancelled:
                    return
                if chunk:
                    await ws.send(json.dumps({"text": f"{chunk} "}))
            if not self._cancelled:
                await ws.send(json.dumps({"text": ""}))
        except Exception:  # noqa: BLE001 — surfaced via the receive loop / close
            return

    async def cancel(self) -> None:
        """Abort in-flight synthesis (V4 barge-in). Idempotent."""
        if self._cancelled:
            return
        self._cancelled = True
        ws = self._active_ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    async def close(self) -> None:
        """Close the backend (idempotent). No persistent connection to tear down
        (sockets are per-utterance); mark closed + drop any active socket."""
        if self._closed:
            return
        self._closed = True
        ws = self._active_ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    # ----- catalogue (VoiceCatalogue Protocol, D-V3-3 / D-V14-4) -------

    async def list_voices(
        self,
        *,
        gender: VoiceGender | None = None,
        language: str | None = None,
        limit: int | None = None,
    ) -> tuple[VoiceCatalogueEntry, ...]:
        """List ElevenLabs voices as data-only records (D-V3-3 / D-V14-4).

        Dialect-aware (D-V14-4): the ``language`` filter matches a voice when the
        base language code is among the voice's supported set (its
        ``labels.language`` + every ``verified_languages`` entry), so a persona
        declaring a language gets voices that actually speak it — and the entry's
        ``description`` is enriched with the accent VERIFIED FOR THAT LANGUAGE
        (not merely the voice's primary accent), so the downstream auto-pick can
        prefer a dialect-appropriate voice (T4). If no voice is verified for the
        language, ALL voices are returned (every ElevenLabs voice can attempt any
        model language — timbre is voice-scoped, language is model-scoped), so a
        rare language still yields choices.
        """
        catalogue = await self._cached_catalogue_raw()
        base = _base_lang(language)
        matched = [v for v in catalogue if base is None or base in _supported_langs(v)]
        # Fallback: no voice verified for this language → offer all (any voice can
        # attempt it via the model). Keeps a rare language from returning nothing.
        source = matched if matched else catalogue
        entries: list[VoiceCatalogueEntry] = []
        for voice in source:
            entry = _to_entry(voice, requested_lang=base)
            if gender is not None and entry.gender != gender:
                continue
            entries.append(entry)
        if limit is not None:
            entries = entries[:limit]
        return tuple(entries)

    async def _cached_catalogue_raw(self) -> list[dict[str, Any]]:
        """Return the raw provider voice records, fetching + caching on a miss."""
        import time

        cache = self._raw_cache
        if cache is not None and (time.monotonic() - self._raw_cache_at) < _CATALOGUE_CACHE_TTL_S:
            return cache
        try:
            raw = await self._http_get_voices()
        except TTSError:
            raise
        except Exception as exc:  # noqa: BLE001 — adapter-boundary mapping
            self._raise_mapped(exc)
        self._raw_cache = raw
        self._raw_cache_at = time.monotonic()
        return raw

    # ----- helpers -----------------------------------------------------

    def _stream_input_url(self, voice_id: str) -> str:
        """Build the per-utterance ``stream-input`` URL (no language_code)."""
        params = urlencode(
            {
                "model_id": self._model,
                "output_format": _OUTPUT_FORMAT,
                "auto_mode": "true",
            }
        )
        return f"{_WS_BASE}/{quote(voice_id, safe='')}/stream-input?{params}"

    def _default_ws_connect(self, ws_url: str) -> _WebSocketCM:
        """Connect to the ElevenLabs stream-input WS via ``websockets`` (lazy)."""
        import websockets

        assert self._config.elevenlabs_api_key is not None  # validated in __init__
        key = self._config.elevenlabs_api_key.get_secret_value()
        # websockets 15.x uses ``additional_headers`` for the handshake headers.
        return websockets.connect(  # type: ignore[return-value]
            ws_url, additional_headers={"xi-api-key": key}
        )

    async def _default_get_voices(self) -> list[dict[str, Any]]:
        """Fetch ``/v1/voices`` via ``httpx`` (lazy import)."""
        import httpx

        assert self._config.elevenlabs_api_key is not None  # validated in __init__
        key = self._config.elevenlabs_api_key.get_secret_value()
        try:
            async with httpx.AsyncClient(timeout=self._config.request_timeout_s) as client:
                resp = await client.get(_VOICES_URL, headers={"xi-api-key": key})
        except httpx.HTTPError as exc:
            raise TTSStreamFailureError(
                "elevenlabs voice-catalogue transport error",
                context={"provider": _PROVIDER_NAME},
            ) from exc
        if resp.status_code in (401, 403):
            raise TTSAuthenticationError(
                "elevenlabs rejected the api key on /v1/voices",
                context={"provider": _PROVIDER_NAME, "status": str(resp.status_code)},
            )
        if resp.status_code >= 400:
            raise TTSStreamFailureError(
                "elevenlabs /v1/voices failed",
                context={"provider": _PROVIDER_NAME, "status": str(resp.status_code)},
            )
        payload = resp.json()
        voices = payload.get("voices", []) if isinstance(payload, dict) else []
        return [v for v in voices if isinstance(v, dict)]

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None]) -> None:
        if task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def _raise_mapped(self, exc: BaseException) -> None:
        """Re-raise a provider/transport exception through the TTS hierarchy."""
        if isinstance(exc, TTSError):
            raise exc
        status = _extract_status(exc)
        if status in (401, 403):
            raise TTSAuthenticationError(
                "elevenlabs rejected the connection",
                context={"provider": _PROVIDER_NAME, "status": str(status)},
            ) from exc
        message = str(exc) or exc.__class__.__name__
        raise TTSStreamFailureError(message, context={"provider": _PROVIDER_NAME}) from exc


# ---------------------------------------------------------------------------
# module-level mapping helpers (pure — unit-testable in isolation)
# ---------------------------------------------------------------------------


def _base_lang(language: str | None) -> str | None:
    """Normalise a raw language/locale to its base ISO-639-1 code (``en-US`` →
    ``en``); ``None``/empty → ``None``."""
    if not language:
        return None
    return language.strip().lower().split("-", 1)[0]


def _supported_langs(voice: dict[str, Any]) -> set[str]:
    """The set of base language codes a voice supports (labels + verified)."""
    langs: set[str] = set()
    labels = voice.get("labels") or {}
    primary = _base_lang(labels.get("language"))
    if primary:
        langs.add(primary)
    for vl in voice.get("verified_languages") or []:
        if isinstance(vl, dict):
            code = _base_lang(vl.get("language"))
            if code:
                langs.add(code)
    return langs


def _verified_accent(voice: dict[str, Any], base: str | None) -> str | None:
    """The accent/locale a voice is verified with FOR ``base`` (the dialect for
    the target language), falling back to the voice's primary label accent."""
    if base is not None:
        for vl in voice.get("verified_languages") or []:
            if isinstance(vl, dict) and _base_lang(vl.get("language")) == base:
                return str(vl.get("accent") or vl.get("locale") or "") or None
    labels = voice.get("labels") or {}
    return str(labels.get("accent") or "") or None


def _to_entry(voice: dict[str, Any], *, requested_lang: str | None) -> VoiceCatalogueEntry:
    """Map one raw ElevenLabs voice record → a :class:`VoiceCatalogueEntry`.

    ``language`` is the requested language when the voice supports it (it is
    being offered FOR that language), else the voice's primary label language.
    ``description`` is enriched with the dialect (accent) verified for the target
    language so the auto-pick can prefer a dialect-appropriate voice (D-V14-4).
    """
    labels = voice.get("labels") or {}
    primary_lang = _base_lang(labels.get("language"))
    offered_lang = requested_lang if requested_lang in _supported_langs(voice) else primary_lang
    accent = _verified_accent(voice, offered_lang)
    base_desc = str(voice.get("description") or "")
    descriptive = str(labels.get("descriptive") or "")
    accent_phrase = f"{accent} accent".strip() if accent else ""
    description = "; ".join(p for p in (base_desc, descriptive, accent_phrase) if p)
    return VoiceCatalogueEntry(
        voice_id=str(voice.get("voice_id") or ""),
        name=str(voice.get("name") or ""),
        gender=normalize_gender(labels.get("gender")),
        language=offered_lang,
        description=description,
        preview_url=voice.get("preview_url"),
    )


def _extract_status(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction from a transport/handshake exception
    (``websockets`` ``InvalidStatus`` exposes ``.response.status_code``)."""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None
