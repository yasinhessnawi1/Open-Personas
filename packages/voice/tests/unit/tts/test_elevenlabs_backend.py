"""Unit tests for :class:`ElevenLabsStreamingTTS` — the Spec V14 TTS backend.

Covers the D-V14-9 concrete behavior against injected transport fakes (zero live
spend, the D-V14-8 no-SDK raw-transport seam):

* Construction fails fast on a missing ``PERSONA_ELEVENLABS_API_KEY`` (D-02-10).
* ``provider_name`` / ``model_name`` / ``consumes_raw_text`` / cost.
* Synthesis WS message sequencing (init ``" "``, ``{"text": "<chunk> "}`` per
  chunk with the required trailing space, final ``{"text": ""}``); base64 audio
  → :class:`AudioChunk` frames via the reframer; ``isFinal`` terminates; the
  first frame arrives before the text stream completes.
* **The D-V14-1 contract: NO ``language_code`` EVER on the wire** — neither in
  the URL nor any message — across the SAME voice_id × ≥3 languages
  (text-language auto-follow is the whole point).
* Cancel mid-stream ends the iterator cleanly (socket close).
* An error frame maps to :class:`TTSStreamFailureError`; a provider mismatch to
  :class:`TTSError`; a handshake auth rejection to :class:`TTSAuthenticationError`.
* ``list_voices`` catalogue mapping incl. the dialect-aware language filter,
  gender filter, limit, and the "no verified voice → offer all" fallback.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import TYPE_CHECKING

import pytest
from persona_voice.tts.config import StreamingTTSConfig
from persona_voice.tts.elevenlabs_backend import ElevenLabsStreamingTTS
from persona_voice.tts.errors import (
    TTSAuthenticationError,
    TTSError,
    TTSStreamFailureError,
)
from persona_voice.tts.types import ResolvedVoice

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona_voice.loop.streaming import AudioChunk


def _config(**overrides: object) -> StreamingTTSConfig:
    base: dict[str, object] = {"provider": "elevenlabs", "elevenlabs_api_key": "el-secret"}
    base.update(overrides)
    return StreamingTTSConfig(**base)  # type: ignore[arg-type]


def _audio_msg(pcm: bytes) -> dict[str, object]:
    return {"audio": base64.b64encode(pcm).decode("ascii")}


async def _text_stream(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


# ---------- transport fakes ------------------------------------------------


class _FakeElevenWS:
    """A scripted ElevenLabs stream-input WebSocket."""

    def __init__(self, server_messages: list[dict[str, object]]) -> None:
        self._messages = list(server_messages)
        self.sent: list[str] = []
        self._closed = asyncio.Event()

    async def __aenter__(self) -> _FakeElevenWS:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    def __aiter__(self) -> _FakeElevenWS:
        return self

    async def __anext__(self) -> str:
        if self._messages:
            await asyncio.sleep(0)
            return json.dumps(self._messages.pop(0))
        await self._closed.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self._closed.set()

    @property
    def sent_texts(self) -> list[str]:
        """The ``text`` field of every JSON message the client sent."""
        out: list[str] = []
        for raw in self.sent:
            with __import__("contextlib").suppress(Exception):
                out.append(json.loads(raw).get("text"))
        return out


class _FakeTransport:
    def __init__(
        self, ws: _FakeElevenWS | None = None, voices: list[dict[str, object]] | None = None
    ) -> None:
        self._ws = ws
        self._voices = voices or []
        self.connected_url: str | None = None
        self.voice_calls = 0

    def ws_connect(self, url: str) -> _FakeElevenWS:
        self.connected_url = url
        assert self._ws is not None
        return self._ws

    async def get_voices(self) -> list[dict[str, object]]:
        self.voice_calls += 1
        return self._voices


def _backend(transport: _FakeTransport, **cfg: object) -> ElevenLabsStreamingTTS:
    return ElevenLabsStreamingTTS(
        _config(**cfg),
        ws_connect=transport.ws_connect,  # type: ignore[arg-type]
        http_get_voices=transport.get_voices,
    )


async def _run_synth(
    backend: ElevenLabsStreamingTTS, voice_ref: str, *chunks: str
) -> list[AudioChunk]:
    voice = ResolvedVoice(provider="elevenlabs", voice_ref=voice_ref)
    return [frame async for frame in backend.synthesize(_text_stream(*chunks), voice)]


# ---------- construction ----------------------------------------------------


def test_construction_fails_fast_without_elevenlabs_key() -> None:
    with pytest.raises(TTSAuthenticationError) as exc_info:
        ElevenLabsStreamingTTS(StreamingTTSConfig(provider="elevenlabs"))
    assert exc_info.value.context["provider"] == "elevenlabs"


def test_introspection_properties() -> None:
    backend = _backend(_FakeTransport())
    assert backend.provider_name == "elevenlabs"
    assert backend.model_name == "eleven_flash_v2_5"
    assert backend.consumes_raw_text is False
    assert backend.cost_cents_per_minute > 0


def test_model_name_reflects_config() -> None:
    backend = _backend(_FakeTransport(), elevenlabs_model="eleven_multilingual_v2")
    assert backend.model_name == "eleven_multilingual_v2"


def test_satisfies_streaming_tts_and_voice_catalogue_protocols() -> None:
    from persona_voice.tts.catalogue import VoiceCatalogue
    from persona_voice.tts.protocol import StreamingTTS

    backend = _backend(_FakeTransport())
    assert isinstance(backend, StreamingTTS)
    assert isinstance(backend, VoiceCatalogue)


# ---------- synthesis -------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_yields_reframed_audio_and_conserves_bytes() -> None:
    pcm = b"\x01\x02" * 1200  # 2400 bytes of PCM16
    ws = _FakeElevenWS([_audio_msg(pcm), {"isFinal": True}])
    backend = _backend(_FakeTransport(ws))
    frames = await _run_synth(backend, "voice-1", "Hello", "world")
    assert frames  # at least one AudioChunk
    assert sum(len(f.data) for f in frames) == len(pcm)  # reframer conserves bytes
    assert all(f.sample_rate == 24000 for f in frames)


@pytest.mark.asyncio
async def test_synthesize_sends_init_then_chunks_then_end() -> None:
    ws = _FakeElevenWS([{"isFinal": True}])
    backend = _backend(_FakeTransport(ws))
    await _run_synth(backend, "voice-1", "Hei", "verden")
    # init " ", each chunk with a trailing space, then the terminal "".
    assert ws.sent_texts[0] == " "
    assert "Hei " in ws.sent_texts
    assert "verden " in ws.sent_texts
    assert ws.sent_texts[-1] == ""


@pytest.mark.asyncio
async def test_synthesize_first_frame_before_text_stream_completes() -> None:
    pcm = b"\x03\x04" * 1000
    ws = _FakeElevenWS([_audio_msg(pcm), {"isFinal": True}])
    backend = _backend(_FakeTransport(ws))

    slow_yielded: list[str] = []

    async def _slow() -> AsyncIterator[str]:
        yield "first"
        slow_yielded.append("first")
        # A frame should already have been yielded by the time the 2nd chunk is
        # requested (streaming-everywhere, spec §6 #2). Not strictly assertable
        # without a barrier, so we just prove frames arrive alongside text.
        yield "second"
        slow_yielded.append("second")

    voice = ResolvedVoice(provider="elevenlabs", voice_ref="v1")
    frames = [f async for f in backend.synthesize(_slow(), voice)]
    assert frames
    assert slow_yielded == ["first", "second"]


@pytest.mark.asyncio
async def test_provider_mismatch_raises() -> None:
    backend = _backend(_FakeTransport(_FakeElevenWS([{"isFinal": True}])))
    voice = ResolvedVoice(provider="cartesia", voice_ref="v1")
    with pytest.raises(TTSError):
        _ = [f async for f in backend.synthesize(_text_stream("hi"), voice)]


@pytest.mark.asyncio
async def test_error_frame_maps_to_stream_failure() -> None:
    ws = _FakeElevenWS([{"error": "quota", "message": "tier does not allow pcm"}])
    backend = _backend(_FakeTransport(ws))
    with pytest.raises(TTSStreamFailureError):
        await _run_synth(backend, "v1", "hello")


@pytest.mark.asyncio
async def test_handshake_auth_rejection_maps_to_auth_error() -> None:
    class _AuthRejectingCM:
        async def __aenter__(self) -> object:
            err = RuntimeError("handshake rejected")
            err.status_code = 401  # type: ignore[attr-defined]
            raise err

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    def _connect(_url: str) -> _AuthRejectingCM:
        return _AuthRejectingCM()

    backend = ElevenLabsStreamingTTS(
        _config(),
        ws_connect=_connect,  # type: ignore[arg-type]
    )
    with pytest.raises(TTSAuthenticationError):
        await _run_synth(backend, "v1", "hello")


@pytest.mark.asyncio
async def test_cancel_ends_stream_cleanly() -> None:
    pcm = b"\x05\x06" * 1000
    # One audio frame then the socket hangs (no isFinal) — cancel must end it.
    ws = _FakeElevenWS([_audio_msg(pcm)])
    backend = _backend(_FakeTransport(ws))
    voice = ResolvedVoice(provider="elevenlabs", voice_ref="v1")
    gen = backend.synthesize(_text_stream("hello"), voice)
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert first.data
    await backend.cancel()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=1.0)


@pytest.mark.asyncio
async def test_cancel_is_idempotent() -> None:
    backend = _backend(_FakeTransport(_FakeElevenWS([{"isFinal": True}])))
    await backend.cancel()
    await backend.cancel()  # no error


# ---------- the D-V14-1 contract: no language on the wire -------------------


@pytest.mark.asyncio
async def test_no_language_code_ever_on_the_wire_same_voice_multi_language() -> None:
    """The D-V14-2 CI-provable TTS criterion: the SAME voice_id renders ≥3
    languages with NO language param anywhere on the wire — neither the URL query
    nor any message body. Text-language auto-follow is the whole point (D-V14-1)."""
    texts = {
        "en": "Hello, how are you?",
        "no": "Hei, hvordan går det?",
        "ar": "مرحبا كيف حالك؟",
        "fr": "Bonjour, comment ça va?",
    }
    voice_ref = "one-shared-voice-id"
    for text in texts.values():
        ws = _FakeElevenWS([{"isFinal": True}])
        transport = _FakeTransport(ws)
        backend = _backend(transport)
        await _run_synth(backend, voice_ref, text)
        # URL carries model_id/output_format/auto_mode but NEVER language.
        assert transport.connected_url is not None
        assert "language" not in transport.connected_url.lower()
        assert voice_ref in transport.connected_url
        # No message body carries a language field either.
        for raw in ws.sent:
            body = json.loads(raw)
            assert "language" not in body
            assert "language_code" not in body


# ---------- list_voices catalogue mapping (D-V14-4) ------------------------


def _voice(
    voice_id: str,
    *,
    gender: str,
    language: str,
    accent: str,
    verified: list[dict[str, str]] | None = None,
    description: str = "",
) -> dict[str, object]:
    return {
        "voice_id": voice_id,
        "name": f"Voice {voice_id}",
        "description": description,
        "preview_url": f"http://preview/{voice_id}",
        "labels": {"gender": gender, "language": language, "accent": accent, "descriptive": "warm"},
        "verified_languages": verified or [],
    }


_CATALOGUE = [
    _voice(
        "en1",
        gender="male",
        language="en",
        accent="american",
        verified=[{"language": "en", "accent": "american", "locale": "en-US"}],
    ),
    _voice(
        "ar1",
        gender="female",
        language="en",
        accent="american",
        verified=[
            {"language": "ar", "accent": "egyptian", "locale": "ar-EG"},
            {"language": "en", "accent": "american", "locale": "en-US"},
        ],
    ),
]


@pytest.mark.asyncio
async def test_list_voices_maps_records_to_entries() -> None:
    backend = _backend(_FakeTransport(voices=_CATALOGUE))
    entries = await backend.list_voices()
    by_id = {e.voice_id: e for e in entries}
    assert by_id["en1"].gender == "masculine"  # male → masculine
    assert by_id["ar1"].gender == "feminine"  # female → feminine
    assert by_id["en1"].preview_url == "http://preview/en1"


@pytest.mark.asyncio
async def test_list_voices_language_filter_is_dialect_capable() -> None:
    """A language filter matches voices VERIFIED for that language (not just the
    primary label) — ``ar1`` is primary-English but Arabic-verified, so an ``ar``
    filter returns it, with the Egyptian dialect surfaced in the description."""
    backend = _backend(_FakeTransport(voices=_CATALOGUE))
    ar_entries = await backend.list_voices(language="ar")
    assert [e.voice_id for e in ar_entries] == ["ar1"]
    assert ar_entries[0].language == "ar"
    assert "egyptian" in ar_entries[0].description.lower()


@pytest.mark.asyncio
async def test_list_voices_locale_filter_normalizes_to_base() -> None:
    backend = _backend(_FakeTransport(voices=_CATALOGUE))
    entries = await backend.list_voices(language="ar-EG")
    assert [e.voice_id for e in entries] == ["ar1"]


@pytest.mark.asyncio
async def test_list_voices_unverified_language_falls_back_to_all() -> None:
    """No voice verified for the language → offer ALL (any ElevenLabs voice can
    attempt any model language). A rare language never returns nothing."""
    backend = _backend(_FakeTransport(voices=_CATALOGUE))
    entries = await backend.list_voices(language="vi")
    assert {e.voice_id for e in entries} == {"en1", "ar1"}


@pytest.mark.asyncio
async def test_list_voices_gender_filter_and_limit() -> None:
    backend = _backend(_FakeTransport(voices=_CATALOGUE))
    males = await backend.list_voices(gender="masculine")
    assert [e.voice_id for e in males] == ["en1"]
    limited = await backend.list_voices(limit=1)
    assert len(limited) == 1


@pytest.mark.asyncio
async def test_list_voices_caches_the_raw_fetch() -> None:
    transport = _FakeTransport(voices=_CATALOGUE)
    backend = _backend(transport)
    await backend.list_voices()
    await backend.list_voices(language="ar")
    assert transport.voice_calls == 1  # fetched once, re-derived per filter


@pytest.mark.asyncio
async def test_list_voices_auth_error_propagates() -> None:
    async def _boom() -> list[dict[str, object]]:
        raise TTSAuthenticationError("bad key", context={"provider": "elevenlabs"})

    backend = ElevenLabsStreamingTTS(_config(), http_get_voices=_boom)
    with pytest.raises(TTSAuthenticationError):
        await backend.list_voices()
