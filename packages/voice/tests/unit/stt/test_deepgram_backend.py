"""Unit tests for :class:`DeepgramStreamingSTT` — T04 launch backend.

Covers the Spec V2 D-V2-1 LOCK launch provider concrete behavior:

* Construction fails fast on missing API key per Spec 02 D-02-10
  (the V2 mirror discipline).
* :meth:`push_audio` is a no-op before the WebSocket opens and rejects
  wrong sample-rates with :class:`STTAudioFormatError` per D-V1-6.
* The first :meth:`push_audio` lazily opens the WebSocket via a
  monkeypatched ``DeepgramClient`` whose connection records the event
  handlers the backend wires.
* Scripted Deepgram event messages (``Transcript`` / ``SpeechStarted``
  / ``UtteranceEnd``) feed through the handlers into the two
  output iterators with the boundary records V4 + V5 expect.
* :meth:`close` is idempotent and drains both iterators cleanly.
* Provider exceptions surface through the
  :class:`persona_voice.stt.errors.STTError` domain hierarchy (401/403
  → :class:`STTAuthenticationError`, 429 → :class:`STTRateLimitError`,
  generic disconnect → :class:`STTStreamFailureError`, audio-format
  rejection → :class:`STTAudioFormatError`).

Tests never touch a real Deepgram WebSocket — the
:class:`_FakeDeepgramClient` + :class:`_FakeConnection` doubles replace
the SDK entry point with a record-and-replay shim that exercises the
backend's event-bus wiring + error-mapping logic.

The ``# ruff: noqa: ANN401, ARG001`` carve-out at the top of the module
mirrors the production-side discipline in
``persona_voice.stt.deepgram_backend`` (the SDK event-bus surface uses
``Any``-typed callback positionals; the ``fake_connection`` fixture is
declared on every test even when only its monkeypatch side effect is
needed — pytest fixture-discovery contract — and ``data`` shadows the SDK
``send(data: bytes | str)`` signature).
"""

# ruff: noqa: ANN401, ARG001

from __future__ import annotations

import os
from typing import Any

import pytest
from persona_voice.loop.streaming import Transcript
from persona_voice.stt import (
    StreamingSTTConfig,
    STTAudioFormatError,
    STTAuthenticationError,
    STTRateLimitError,
    STTStreamFailureError,
)
from persona_voice.stt.deepgram_backend import DeepgramStreamingSTT, transcribe_prerecorded
from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent

# ---------- env hygiene ----------------------------------------------------


@pytest.fixture(autouse=True)
def _strip_persona_stt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("PERSONA_STT_"):
            monkeypatch.delenv(key, raising=False)


# ---------- fake Deepgram SDK doubles -------------------------------------


class _FakeAlternative:
    def __init__(self, transcript: str, confidence: float) -> None:
        self.transcript = transcript
        self.confidence = confidence


class _FakeChannel:
    def __init__(self, alternatives: list[_FakeAlternative]) -> None:
        self.alternatives = alternatives


class _FakeResult:
    def __init__(
        self,
        *,
        text: str,
        confidence: float,
        is_final: bool,
        speech_final: bool,
    ) -> None:
        self.channel = _FakeChannel([_FakeAlternative(text, confidence)])
        self.is_final = is_final
        self.speech_final = speech_final


class _FakeSpeechStarted:
    def __init__(self, timestamp: float = 0.5) -> None:
        self.timestamp = timestamp


class _FakeUtteranceEnd:
    def __init__(self, last_word_end: float = 1.2) -> None:
        self.last_word_end = last_word_end
        self.start = last_word_end  # backend reads `start` defensively


class _FakeConnection:
    """Records handler wiring + emits scripted events at the test's command."""

    def __init__(self, *, start_result: bool = True) -> None:
        self.handlers: dict[Any, Any] = {}
        self.sent: list[bytes | str] = []
        self.finish_calls = 0
        self.start_calls: list[Any] = []
        self._start_result = start_result

    def on(self, event: Any, handler: Any) -> None:
        self.handlers[event] = handler

    async def start(self, options: Any) -> bool:
        self.start_calls.append(options)
        return self._start_result

    async def send(self, data: bytes | str) -> bool:
        self.sent.append(data)
        return True

    async def finish(self) -> bool:
        self.finish_calls += 1
        return True

    async def emit(self, event: Any, payload: Any) -> None:
        """Test helper — invoke the wired handler for ``event``."""
        handler = self.handlers[event]
        await handler(self, payload)


class _FakeAsyncWebsocket:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def v(self, _version: str) -> _FakeConnection:
        return self._connection


class _FakeListen:
    def __init__(self, connection: _FakeConnection) -> None:
        self.asyncwebsocket = _FakeAsyncWebsocket(connection)


class _FakeDeepgramClient:
    def __init__(
        self,
        api_key: str = "",
        config: Any | None = None,
        access_token: str = "",
    ) -> None:
        self.api_key = api_key
        self.config = config
        self.access_token = access_token
        self.listen = _FakeListen(_FAKE_CONNECTION_REGISTRY[-1])


# Module-level registry so each test owns a freshly-injected connection.
_FAKE_CONNECTION_REGISTRY: list[_FakeConnection] = []


@pytest.fixture
def fake_connection(monkeypatch: pytest.MonkeyPatch) -> _FakeConnection:
    """Inject a fresh :class:`_FakeConnection` for the test.

    The backend lazy-imports ``DeepgramClient`` inside ``_open_connection``
    via ``from deepgram import DeepgramClient, ...``. Patching the
    ``deepgram`` module attribute (NOT the backend module) is what
    intercepts that import — ``from X import Y`` resolves ``Y`` against
    the patched ``X`` namespace.
    """
    import deepgram

    connection = _FakeConnection()
    _FAKE_CONNECTION_REGISTRY.append(connection)
    monkeypatch.setattr(deepgram, "DeepgramClient", _FakeDeepgramClient)
    yield connection
    _FAKE_CONNECTION_REGISTRY.pop()


# ---------- construction --------------------------------------------------


def test_construction_without_api_key_raises_authentication_error() -> None:
    """Spec 02 D-02-10 fail-fast: no key → :class:`STTAuthenticationError`."""
    config = StreamingSTTConfig(provider="deepgram")
    with pytest.raises(STTAuthenticationError) as exc_info:
        DeepgramStreamingSTT(config)
    assert exc_info.value.context["provider"] == "deepgram"
    assert "PERSONA_STT_API_KEY" in str(exc_info.value)


def test_construction_with_empty_api_key_raises_authentication_error() -> None:
    """``SecretStr("")`` is treated the same as missing — empty key fails fast."""
    config = StreamingSTTConfig(provider="deepgram", api_key="")
    with pytest.raises(STTAuthenticationError):
        DeepgramStreamingSTT(config)


def test_construction_with_api_key_stores_config() -> None:
    """Successful construction exposes :attr:`provider_name` + :attr:`model_name`."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret", model="nova-3")
    backend = DeepgramStreamingSTT(config)
    assert backend.provider_name == "deepgram"
    assert backend.model_name == "nova-3"


# ---------- push_audio --------------------------------------------------


@pytest.mark.asyncio
async def test_push_audio_after_close_is_noop(
    fake_connection: _FakeConnection,
) -> None:
    """After :meth:`close`, :meth:`push_audio` returns silently."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.close()
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    assert fake_connection.sent == []


@pytest.mark.asyncio
async def test_push_audio_rejects_wrong_sample_rate(
    fake_connection: _FakeConnection,
) -> None:
    """Sample-rate negotiation: 16 kHz only per D-V1-6."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    with pytest.raises(STTAudioFormatError) as exc_info:
        await backend.push_audio(b"\x00\x00" * 320, 48000)
    assert exc_info.value.context["provider"] == "deepgram"
    assert exc_info.value.context["sample_rate"] == "48000"


@pytest.mark.asyncio
async def test_push_audio_lazily_opens_connection(
    fake_connection: _FakeConnection,
) -> None:
    """First :meth:`push_audio` opens the WebSocket; later calls just send."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    await backend.push_audio(b"\x01\x00" * 320, 16000)
    assert len(fake_connection.start_calls) == 1
    assert fake_connection.sent == [b"\x00\x00" * 320, b"\x01\x00" * 320]


@pytest.mark.asyncio
async def test_open_connection_failure_raises_stream_failure(
    fake_connection: _FakeConnection,
) -> None:
    """``connection.start()`` returning ``False`` → :class:`STTStreamFailureError`."""
    fake_connection._start_result = False
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    with pytest.raises(STTStreamFailureError):
        await backend.push_audio(b"\x00\x00" * 320, 16000)


# ---------- transcripts() ----------------------------------------------


@pytest.mark.asyncio
async def test_transcripts_emits_partial_then_final(
    fake_connection: _FakeConnection,
) -> None:
    """Partial (``is_final=False``) + Final (``is_final=True``) both yielded.

    ``speech_final=True`` populates ``eou_at`` on the final transcript so
    T08's stt_partial_first_at / stt_audio_pushed_at hops can sample it.
    """
    from deepgram import LiveTranscriptionEvents

    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.push_audio(b"\x00\x00" * 320, 16000)

    partial = _FakeResult(text="hello", confidence=0.8, is_final=False, speech_final=False)
    final = _FakeResult(text="hello world", confidence=0.95, is_final=True, speech_final=True)
    await fake_connection.emit(LiveTranscriptionEvents.Transcript, partial)
    await fake_connection.emit(LiveTranscriptionEvents.Transcript, final)
    await backend.close()

    collected: list[Transcript] = []
    async for t in backend.transcripts():
        collected.append(t)
    assert len(collected) == 2
    assert collected[0] == Transcript(is_final=False, text="hello", confidence=0.8, eou_at=None)
    assert collected[1].is_final is True
    assert collected[1].text == "hello world"
    assert collected[1].eou_at is not None  # speech_final = True populates it


@pytest.mark.asyncio
async def test_transcripts_skips_empty_text(
    fake_connection: _FakeConnection,
) -> None:
    """Deepgram sometimes emits empty-transcript heartbeats — skip them."""
    from deepgram import LiveTranscriptionEvents

    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.push_audio(b"\x00\x00" * 320, 16000)

    empty = _FakeResult(text="", confidence=0.0, is_final=False, speech_final=False)
    real = _FakeResult(text="hi", confidence=0.9, is_final=True, speech_final=True)
    await fake_connection.emit(LiveTranscriptionEvents.Transcript, empty)
    await fake_connection.emit(LiveTranscriptionEvents.Transcript, real)
    await backend.close()

    collected = [t async for t in backend.transcripts()]
    assert len(collected) == 1
    assert collected[0].text == "hi"


# ---------- speech_activity_events() -----------------------------------


@pytest.mark.asyncio
async def test_speech_started_event_maps_to_provider_source(
    fake_connection: _FakeConnection,
) -> None:
    """``SpeechStarted`` → :class:`SpeechStartedEvent` with ``source="provider"``."""
    from deepgram import LiveTranscriptionEvents

    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.push_audio(b"\x00\x00" * 320, 16000)

    await fake_connection.emit(
        LiveTranscriptionEvents.SpeechStarted, _FakeSpeechStarted(timestamp=0.42)
    )
    await backend.close()

    events = [e async for e in backend.speech_activity_events()]
    assert len(events) == 1
    assert isinstance(events[0], SpeechStartedEvent)
    assert events[0].source == "provider"
    assert events[0].ts_audio_s == pytest.approx(0.42)


@pytest.mark.asyncio
async def test_utterance_end_event_maps_to_provider_source(
    fake_connection: _FakeConnection,
) -> None:
    """``UtteranceEnd`` → :class:`SpeechEndedEvent` with ``source="provider"``."""
    from deepgram import LiveTranscriptionEvents

    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.push_audio(b"\x00\x00" * 320, 16000)

    await fake_connection.emit(
        LiveTranscriptionEvents.UtteranceEnd, _FakeUtteranceEnd(last_word_end=2.0)
    )
    await backend.close()

    events = [e async for e in backend.speech_activity_events()]
    assert len(events) == 1
    assert isinstance(events[0], SpeechEndedEvent)
    assert events[0].source == "provider"
    assert events[0].corroborates is False  # T06 seam adapter sets it


# ---------- close() ----------------------------------------------------


@pytest.mark.asyncio
async def test_close_is_idempotent(
    fake_connection: _FakeConnection,
) -> None:
    """Second :meth:`close` is a no-op per the Protocol docstring contract."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    await backend.close()
    await backend.close()
    # finish() called once (the post-connect path); second close is a no-op.
    assert fake_connection.finish_calls == 1


@pytest.mark.asyncio
async def test_close_before_open_terminates_iterators(
    fake_connection: _FakeConnection,
) -> None:
    """Close without ever opening the WebSocket still drains iterators."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.close()
    collected = [t async for t in backend.transcripts()]
    events = [e async for e in backend.speech_activity_events()]
    assert collected == []
    assert events == []


@pytest.mark.asyncio
async def test_server_close_terminates_without_finish(
    fake_connection: _FakeConnection,
) -> None:
    """A server-side ``Close`` (the 1011 idle-close) drains the iterators
    WITHOUT calling ``finish()``.

    Awaiting ``connection.finish()`` from inside the SDK's ``Close`` callback —
    which runs on the SDK's own listening task — cancels that task from within
    itself and recurses into ``RecursionError`` (the idle-close crash). The
    server has already closed the socket, so the handler must only terminate the
    iterators, never re-finish.
    """
    from deepgram import LiveTranscriptionEvents

    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.push_audio(b"\x00\x00" * 320, 16000)  # opens the connection
    # The server closes the stream (idle 1011) → fire the wired Close handler.
    await fake_connection.emit(LiveTranscriptionEvents.Close, None)
    # finish() NOT called (no self-cancelling recursion); iterators terminate.
    assert fake_connection.finish_calls == 0
    collected = [t async for t in backend.transcripts()]
    assert collected == []
    # A subsequent caller close() is now a no-op (already closed by the server).
    await backend.close()
    assert fake_connection.finish_calls == 0


@pytest.mark.asyncio
async def test_keepalive_task_started_on_open(
    fake_connection: _FakeConnection,
) -> None:
    """Opening the stream starts a keepalive task (holds it open across turns);
    close cancels it."""
    import asyncio

    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    task = backend._keepalive_task  # noqa: SLF001 — white-box lifecycle assertion
    assert task is not None
    assert not task.done()
    await backend.close()
    assert backend._keepalive_task is None  # noqa: SLF001
    await asyncio.sleep(0)  # let the requested cancellation settle
    assert task.cancelled()


# ---------- error mapping ----------------------------------------------


class _FakeHttpError(Exception):
    """Stand-in for a transport-layer HTTP error with ``status`` attribute."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class _FakeDeepgramApiKeyError(Exception):
    """Stand-in for ``deepgram.DeepgramApiKeyError`` (the SDK auth error)."""


# Rename so the backend's ``exc.__class__.__name__`` check matches.
_FakeDeepgramApiKeyError.__name__ = "DeepgramApiKeyError"


@pytest.mark.asyncio
async def test_error_mapping_401_to_authentication_error(
    fake_connection: _FakeConnection,
) -> None:
    """HTTP 401 from the provider maps to :class:`STTAuthenticationError`."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    with pytest.raises(STTAuthenticationError) as exc_info:
        backend._raise_mapped(_FakeHttpError("401", "unauthorized"))
    assert exc_info.value.context["status"] == "401"
    assert exc_info.value.context["provider"] == "deepgram"


@pytest.mark.asyncio
async def test_error_mapping_403_to_authentication_error(
    fake_connection: _FakeConnection,
) -> None:
    """HTTP 403 is the other auth status the mapping covers."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    with pytest.raises(STTAuthenticationError):
        backend._raise_mapped(_FakeHttpError("403", "forbidden"))


@pytest.mark.asyncio
async def test_error_mapping_api_key_error_class_to_authentication_error(
    fake_connection: _FakeConnection,
) -> None:
    """SDK-class-name match for ``DeepgramApiKeyError`` → auth error."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    with pytest.raises(STTAuthenticationError):
        backend._raise_mapped(_FakeDeepgramApiKeyError("bad key"))


@pytest.mark.asyncio
async def test_error_mapping_429_to_rate_limit_error(
    fake_connection: _FakeConnection,
) -> None:
    """HTTP 429 → :class:`STTRateLimitError`."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    with pytest.raises(STTRateLimitError) as exc_info:
        backend._raise_mapped(_FakeHttpError("429", "too many requests"))
    assert exc_info.value.context["status"] == "429"


@pytest.mark.asyncio
async def test_error_mapping_400_format_error_to_audio_format_error(
    fake_connection: _FakeConnection,
) -> None:
    """HTTP 400 with audio-format diagnostics → :class:`STTAudioFormatError`."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    with pytest.raises(STTAudioFormatError):
        backend._raise_mapped(_FakeHttpError("400", "invalid encoding=linear16"))


@pytest.mark.asyncio
async def test_error_mapping_disconnect_to_stream_failure(
    fake_connection: _FakeConnection,
) -> None:
    """Generic provider error → :class:`STTStreamFailureError`."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    with pytest.raises(STTStreamFailureError):
        backend._raise_mapped(RuntimeError("websocket disconnected"))


@pytest.mark.asyncio
async def test_error_mapping_passes_domain_exceptions_unchanged(
    fake_connection: _FakeConnection,
) -> None:
    """Domain-hierarchy exceptions raised by the backend pass through."""
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    original = STTAudioFormatError("nope", context={"provider": "deepgram"})
    with pytest.raises(STTAudioFormatError) as exc_info:
        backend._raise_mapped(original)
    assert exc_info.value is original


@pytest.mark.asyncio
async def test_send_failure_maps_to_stream_failure(
    fake_connection: _FakeConnection,
) -> None:
    """Mid-stream send failure → :class:`STTStreamFailureError` from :meth:`push_audio`."""

    async def _boom(data: Any) -> bool:
        raise RuntimeError("connection dropped")

    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    backend = DeepgramStreamingSTT(config)
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    fake_connection.send = _boom  # type: ignore[method-assign]
    with pytest.raises(STTStreamFailureError):
        await backend.push_audio(b"\x01\x00" * 320, 16000)


# ---------- transcribe_prerecorded() (R9-025a one-shot REST /v1/stt) ------
#
# A DISTINCT SDK surface from the WebSocket path above (``listen.asyncrest``,
# not ``listen.asyncwebsocket``) — separate fakes, but the SAME
# ``_FakeHttpError`` / ``_FakeDeepgramApiKeyError`` doubles feed the SAME
# shared ``_raise_mapped_deepgram_error`` mapping the WS tests above already
# cover exhaustively (401/403/429/400/generic); these tests confirm the
# one-shot function routes provider failures through that SAME mapping
# rather than re-asserting every status code again.


class _FakeAlternativeResp:
    def __init__(self, transcript: str) -> None:
        self.transcript = transcript


class _FakeChannelResp:
    def __init__(self, alternatives: list[_FakeAlternativeResp]) -> None:
        self.alternatives = alternatives


class _FakeResultsResp:
    def __init__(self, channels: list[_FakeChannelResp]) -> None:
        self.channels = channels


class _FakePrerecordedResponse:
    """Stand-in for Deepgram's ``PrerecordedResponse`` (only the fields read)."""

    def __init__(self, transcript: str) -> None:
        self.results = _FakeResultsResp([_FakeChannelResp([_FakeAlternativeResp(transcript)])])


class _FakeAsyncRestClient:
    """Records ``transcribe_file`` calls; scripted response OR error."""

    def __init__(self) -> None:
        self.response: Any = _FakePrerecordedResponse("")
        self.error: BaseException | None = None
        self.calls: list[dict[str, Any]] = []

    async def transcribe_file(self, source: Any, options: Any, *, headers: Any = None) -> Any:
        self.calls.append({"source": source, "options": options, "headers": headers})
        if self.error is not None:
            raise self.error
        return self.response


class _FakeAsyncRestNamespace:
    def __init__(self, client: _FakeAsyncRestClient) -> None:
        self._client = client

    def v(self, _version: str) -> _FakeAsyncRestClient:
        return self._client


class _FakeListenPrerecorded:
    def __init__(self, client: _FakeAsyncRestClient) -> None:
        self.asyncrest = _FakeAsyncRestNamespace(client)


class _FakeDeepgramClientPrerecorded:
    def __init__(self, api_key: str = "", config: Any | None = None) -> None:
        self.api_key = api_key
        self.config = config
        self.listen = _FakeListenPrerecorded(_PRERECORDED_REGISTRY[-1])


# Module-level registry so each test owns a freshly-injected REST client
# (mirrors ``_FAKE_CONNECTION_REGISTRY`` above for the WS path).
_PRERECORDED_REGISTRY: list[_FakeAsyncRestClient] = []


@pytest.fixture
def fake_rest_client(monkeypatch: pytest.MonkeyPatch) -> _FakeAsyncRestClient:
    """Inject a fresh :class:`_FakeAsyncRestClient` for the test.

    ``transcribe_prerecorded`` lazy-imports ``DeepgramClient`` inside its own
    body via ``from deepgram import DeepgramClient, ...`` — patching the
    ``deepgram`` module attribute (NOT the backend module) is what
    intercepts that import, same technique as ``fake_connection`` above.
    """
    import deepgram

    client = _FakeAsyncRestClient()
    _PRERECORDED_REGISTRY.append(client)
    monkeypatch.setattr(deepgram, "DeepgramClient", _FakeDeepgramClientPrerecorded)
    yield client
    _PRERECORDED_REGISTRY.pop()


@pytest.mark.asyncio
async def test_transcribe_prerecorded_without_api_key_raises_authentication_error() -> None:
    """Spec 02 D-02-10 fail-fast, mirroring :meth:`DeepgramStreamingSTT.__init__`."""
    config = StreamingSTTConfig(provider="deepgram")
    with pytest.raises(STTAuthenticationError) as exc_info:
        await transcribe_prerecorded(b"fake-audio-bytes", config=config)
    assert exc_info.value.context["provider"] == "deepgram"
    assert "PERSONA_STT_API_KEY" in str(exc_info.value)


@pytest.mark.asyncio
async def test_transcribe_prerecorded_returns_best_alternative_transcript(
    fake_rest_client: _FakeAsyncRestClient,
) -> None:
    fake_rest_client.response = _FakePrerecordedResponse("hello world")
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    transcript = await transcribe_prerecorded(b"pcm-bytes", config=config)
    assert transcript == "hello world"


@pytest.mark.asyncio
async def test_transcribe_prerecorded_sends_the_raw_audio_as_a_buffer_source(
    fake_rest_client: _FakeAsyncRestClient,
) -> None:
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    await transcribe_prerecorded(b"raw-pcm-bytes", config=config)
    assert fake_rest_client.calls[0]["source"] == {"buffer": b"raw-pcm-bytes"}


@pytest.mark.asyncio
async def test_transcribe_prerecorded_forwards_content_type_as_header(
    fake_rest_client: _FakeAsyncRestClient,
) -> None:
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    await transcribe_prerecorded(b"pcm-bytes", config=config, content_type="audio/webm")
    assert fake_rest_client.calls[0]["headers"] == {"Content-Type": "audio/webm"}


@pytest.mark.asyncio
async def test_transcribe_prerecorded_no_content_type_omits_headers(
    fake_rest_client: _FakeAsyncRestClient,
) -> None:
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    await transcribe_prerecorded(b"pcm-bytes", config=config)
    assert fake_rest_client.calls[0]["headers"] is None


@pytest.mark.asyncio
async def test_transcribe_prerecorded_empty_transcript_is_not_an_error(
    fake_rest_client: _FakeAsyncRestClient,
) -> None:
    """A silent/empty clip yields ``""`` — not an :class:`STTError`."""
    fake_rest_client.response = _FakePrerecordedResponse("")
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    transcript = await transcribe_prerecorded(b"silence", config=config)
    assert transcript == ""


@pytest.mark.asyncio
async def test_transcribe_prerecorded_malformed_response_returns_empty_string(
    fake_rest_client: _FakeAsyncRestClient,
) -> None:
    """Defensive extraction: no ``.results`` at all → ``""``, never a crash."""
    fake_rest_client.response = object()
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    transcript = await transcribe_prerecorded(b"pcm-bytes", config=config)
    assert transcript == ""


@pytest.mark.asyncio
async def test_transcribe_prerecorded_maps_401_to_authentication_error(
    fake_rest_client: _FakeAsyncRestClient,
) -> None:
    fake_rest_client.error = _FakeHttpError("401", "unauthorized")
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    with pytest.raises(STTAuthenticationError) as exc_info:
        await transcribe_prerecorded(b"pcm-bytes", config=config)
    assert exc_info.value.context["provider"] == "deepgram"


@pytest.mark.asyncio
async def test_transcribe_prerecorded_maps_429_to_rate_limit_error(
    fake_rest_client: _FakeAsyncRestClient,
) -> None:
    fake_rest_client.error = _FakeHttpError("429", "too many requests")
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    with pytest.raises(STTRateLimitError):
        await transcribe_prerecorded(b"pcm-bytes", config=config)


@pytest.mark.asyncio
async def test_transcribe_prerecorded_maps_generic_failure_to_stream_failure_error(
    fake_rest_client: _FakeAsyncRestClient,
) -> None:
    fake_rest_client.error = RuntimeError("transport dropped")
    config = StreamingSTTConfig(provider="deepgram", api_key="dg-secret")
    with pytest.raises(STTStreamFailureError):
        await transcribe_prerecorded(b"pcm-bytes", config=config)
