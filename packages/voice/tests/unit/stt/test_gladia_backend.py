"""Unit tests for :class:`GladiaStreamingSTT` — the Spec V14 code-switching backend.

Covers the D-V14-9 concrete behavior against injected transport fakes (zero
live spend, the D-V14-8 no-SDK raw-transport seam):

* Construction fails fast on a missing ``PERSONA_GLADIA_API_KEY`` (D-02-10).
* ``provider_name`` / ``model_name`` reflect the config.
* :meth:`push_audio` is a no-op after close, rejects non-16 kHz frames, and
  lazily opens the session + socket then forwards the frame as binary.
* Scripted Gladia ``transcript`` messages feed the transcript iterator with the
  right ``is_final`` / ``eou_at`` / clamped-confidence shape; empty/malformed/
  non-transcript messages are ignored.
* :meth:`close` sends ``stop_recording``, is idempotent, drains cleanly, and
  works before the socket ever opened.
* The provider-side speech-activity stream is EMPTY at v1 (Protocol-permitted).
* Session-init HTTP status + transport errors map onto the STTError hierarchy.
* Reconnect-on-push (the idle-close mitigation): an UNEXPECTED session end does
  NOT terminate the iterators; the next :meth:`push_audio` opens a fresh session.

The fakes replace the two constructor seams (``open_session`` + ``ws_connect``)
so the full message-handling + lifecycle logic runs without a real Gladia
socket.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from persona_voice.stt import (
    StreamingSTTConfig,
    STTAudioFormatError,
    STTAuthenticationError,
    STTError,
    STTRateLimitError,
    STTStreamFailureError,
)
from persona_voice.stt.gladia_backend import (
    GladiaStreamingSTT,
    _raise_for_gladia_status,
    _raise_mapped_gladia_error,
)


def _config(**overrides: object) -> StreamingSTTConfig:
    base: dict[str, object] = {"provider": "gladia", "gladia_api_key": "gl-secret"}
    base.update(overrides)
    return StreamingSTTConfig(**base)  # type: ignore[arg-type]


def _transcript_msg(
    text: str, *, is_final: bool, language: str = "en", confidence: float = 0.9
) -> str:
    return json.dumps(
        {
            "type": "transcript",
            "data": {
                "is_final": is_final,
                "utterance": {"text": text, "language": language, "confidence": confidence},
            },
        }
    )


# ---------- transport fakes ------------------------------------------------


class _FakeGladiaWS:
    """A scripted Gladia WebSocket: delivers pre-loaded server messages, then
    stays open until the client sends ``stop_recording`` (or a forced close)."""

    def __init__(self, messages: list[str] | None = None) -> None:
        self._messages = list(messages or [])
        self.sent: list[bytes | str] = []
        self._close_evt = asyncio.Event()
        self.entered = False
        self.send_should_raise: Exception | None = None

    async def __aenter__(self) -> _FakeGladiaWS:
        self.entered = True
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def send(self, data: bytes | str) -> None:
        if self.send_should_raise is not None:
            raise self.send_should_raise
        self.sent.append(data)
        if isinstance(data, str) and "stop_recording" in data:
            self._close_evt.set()

    def __aiter__(self) -> _FakeGladiaWS:
        return self

    async def __anext__(self) -> str:
        if self._messages:
            await asyncio.sleep(0)
            return self._messages.pop(0)
        await self._close_evt.wait()
        raise StopAsyncIteration

    def force_server_close(self) -> None:
        """Simulate an unexpected server-side drop (e.g. an idle-close)."""
        self._close_evt.set()


class _FakeGladia:
    """Hands out pre-built fake sockets + records session-open calls."""

    def __init__(
        self, sessions: list[_FakeGladiaWS], *, open_error: Exception | None = None
    ) -> None:
        self._sessions = list(sessions)
        self._open_error = open_error
        self.open_calls = 0
        self.handed: list[_FakeGladiaWS] = []

    async def open_session(self) -> tuple[str, str]:
        self.open_calls += 1
        if self._open_error is not None:
            raise self._open_error
        return (f"sid-{self.open_calls}", f"wss://fake/{self.open_calls}")

    def ws_connect(self, url: str) -> _FakeGladiaWS:  # noqa: ARG002 — url unused by the fake
        ws = self._sessions.pop(0)
        self.handed.append(ws)
        return ws


def _backend(fake: _FakeGladia, **cfg_overrides: object) -> GladiaStreamingSTT:
    return GladiaStreamingSTT(
        _config(**cfg_overrides),
        open_session=fake.open_session,
        ws_connect=fake.ws_connect,  # type: ignore[arg-type]
    )


# ---------- construction ----------------------------------------------------


def test_construction_fails_fast_without_gladia_key() -> None:
    with pytest.raises(STTAuthenticationError) as exc_info:
        GladiaStreamingSTT(StreamingSTTConfig(provider="gladia"))
    assert exc_info.value.context["provider"] == "gladia"


def test_construction_fails_fast_with_empty_gladia_key() -> None:
    with pytest.raises(STTAuthenticationError):
        GladiaStreamingSTT(StreamingSTTConfig(provider="gladia", gladia_api_key=""))


def test_gladia_backend_satisfies_streaming_stt_protocol() -> None:
    """The concrete backend structurally satisfies the ``@runtime_checkable``
    :class:`StreamingSTT` Protocol — the only surface V2 callers depend on."""
    from persona_voice.stt import StreamingSTT

    assert isinstance(_backend(_FakeGladia([])), StreamingSTT)


def test_provider_and_model_names() -> None:
    backend = _backend(_FakeGladia([]))
    assert backend.provider_name == "gladia"
    assert backend.model_name == "solaria-1"


def test_model_name_reflects_config() -> None:
    backend = _backend(_FakeGladia([]), gladia_model="solaria-2")
    assert backend.model_name == "solaria-2"


# ---------- push_audio guards ----------------------------------------------


@pytest.mark.asyncio
async def test_push_audio_after_close_is_noop() -> None:
    fake = _FakeGladia([])
    backend = _backend(fake)
    await backend.close()
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    assert fake.open_calls == 0


@pytest.mark.asyncio
async def test_push_audio_rejects_wrong_sample_rate() -> None:
    backend = _backend(_FakeGladia([_FakeGladiaWS()]))
    with pytest.raises(STTAudioFormatError) as exc_info:
        await backend.push_audio(b"\x00\x00" * 160, 8000)
    assert exc_info.value.context["sample_rate"] == "8000"
    assert exc_info.value.context["provider"] == "gladia"


@pytest.mark.asyncio
async def test_push_audio_lazily_opens_session_and_sends_binary_frame() -> None:
    ws = _FakeGladiaWS()
    fake = _FakeGladia([ws])
    backend = _backend(fake)
    frame = b"\x01\x02" * 320
    await backend.push_audio(frame, 16000)
    assert fake.open_calls == 1
    assert ws.entered is True
    assert ws.sent == [frame]
    await backend.close()


# ---------- transcript flow -------------------------------------------------


@pytest.mark.asyncio
async def test_transcripts_partial_then_final() -> None:
    ws = _FakeGladiaWS(
        [
            _transcript_msg("hei", is_final=False, language="no"),
            _transcript_msg("hei der", is_final=True, language="no"),
        ]
    )
    backend = _backend(_FakeGladia([ws]))
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    await backend.close()
    collected = [t async for t in backend.transcripts()]
    assert [t.text for t in collected] == ["hei", "hei der"]
    assert collected[0].is_final is False
    assert collected[0].eou_at is None
    assert collected[1].is_final is True
    assert collected[1].eou_at is not None  # finals stamp end-of-utterance


@pytest.mark.asyncio
async def test_transcripts_skips_empty_malformed_and_non_transcript() -> None:
    ws = _FakeGladiaWS(
        [
            "not json at all",
            json.dumps({"type": "speech_start", "data": {}}),  # non-transcript type
            _transcript_msg("", is_final=True),  # empty text
            json.dumps({"type": "transcript", "data": {}}),  # missing utterance
            _transcript_msg("real words", is_final=True),
        ]
    )
    backend = _backend(_FakeGladia([ws]))
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    await backend.close()
    collected = [t async for t in backend.transcripts()]
    assert [t.text for t in collected] == ["real words"]


@pytest.mark.asyncio
async def test_confidence_defaults_to_one_when_absent_and_clamps() -> None:
    ws = _FakeGladiaWS(
        [
            json.dumps(
                {"type": "transcript", "data": {"is_final": True, "utterance": {"text": "a"}}}
            ),
            json.dumps(
                {
                    "type": "transcript",
                    "data": {"is_final": True, "utterance": {"text": "b", "confidence": 5.0}},
                }
            ),
        ]
    )
    backend = _backend(_FakeGladia([ws]))
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    await backend.close()
    collected = [t async for t in backend.transcripts()]
    assert collected[0].confidence == 1.0  # absent → neutral 1.0
    assert collected[1].confidence == 1.0  # 5.0 clamped into [0,1]


# ---------- close semantics -------------------------------------------------


@pytest.mark.asyncio
async def test_close_sends_stop_recording() -> None:
    ws = _FakeGladiaWS()
    backend = _backend(_FakeGladia([ws]))
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    await backend.close()
    assert any(isinstance(m, str) and "stop_recording" in m for m in ws.sent)


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    ws = _FakeGladiaWS()
    backend = _backend(_FakeGladia([ws]))
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    await backend.close()
    await backend.close()  # second close is a no-op
    stop_count = sum(1 for m in ws.sent if isinstance(m, str) and "stop_recording" in m)
    assert stop_count == 1


@pytest.mark.asyncio
async def test_close_before_open_terminates_iterators() -> None:
    backend = _backend(_FakeGladia([]))
    await backend.close()
    transcripts = [t async for t in backend.transcripts()]
    events = [e async for e in backend.speech_activity_events()]
    assert transcripts == []
    assert events == []


@pytest.mark.asyncio
async def test_speech_activity_events_is_empty_stream() -> None:
    ws = _FakeGladiaWS([_transcript_msg("hi", is_final=True)])
    backend = _backend(_FakeGladia([ws]))
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    await backend.close()
    events = [e async for e in backend.speech_activity_events()]
    assert events == []  # v1 emits no provider-side activity (Silero authoritative)


# ---------- session-init error mapping -------------------------------------


@pytest.mark.asyncio
async def test_session_init_auth_error_surfaces_at_push() -> None:
    fake = _FakeGladia(
        [], open_error=STTAuthenticationError("bad key", context={"provider": "gladia"})
    )
    backend = _backend(fake)
    with pytest.raises(STTAuthenticationError):
        await backend.push_audio(b"\x00\x00" * 320, 16000)


@pytest.mark.asyncio
async def test_session_init_generic_error_maps_to_stream_failure() -> None:
    fake = _FakeGladia([], open_error=RuntimeError("boom"))
    backend = _backend(fake)
    with pytest.raises(STTStreamFailureError):
        await backend.push_audio(b"\x00\x00" * 320, 16000)


@pytest.mark.asyncio
async def test_connect_timeout_maps_to_stream_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("persona_voice.stt.gladia_backend._CONNECT_TIMEOUT_S", 0.05)

    async def _slow_open() -> tuple[str, str]:
        await asyncio.sleep(1.0)
        return ("sid", "wss://fake/1")

    backend = GladiaStreamingSTT(
        _config(),
        open_session=_slow_open,
        ws_connect=_FakeGladia([_FakeGladiaWS()]).ws_connect,  # type: ignore[arg-type]
    )
    with pytest.raises(STTStreamFailureError):
        await backend.push_audio(b"\x00\x00" * 320, 16000)


# ---------- reconnect-on-push (idle-close mitigation) ----------------------


@pytest.mark.asyncio
async def test_reconnect_on_push_after_unexpected_session_end() -> None:
    ws1 = _FakeGladiaWS([_transcript_msg("first", is_final=True)])
    ws2 = _FakeGladiaWS([_transcript_msg("second", is_final=True)])
    fake = _FakeGladia([ws1, ws2])
    backend = _backend(fake)

    # Session 1 opens, delivers "first".
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    task1 = backend._session_task  # noqa: SLF001 — white-box lifecycle assertion
    assert task1 is not None

    # Simulate an unexpected server drop (an idle-close). The receiver ends but
    # the iterators are NOT terminated (reconnect mode).
    ws1.force_server_close()
    await asyncio.wait_for(task1, timeout=1.0)
    assert fake.open_calls == 1

    # The next frame transparently opens a FRESH session (session 2).
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    assert fake.open_calls == 2
    assert ws2.entered is True

    await backend.close()
    collected = [t async for t in backend.transcripts()]
    assert [t.text for t in collected] == ["first", "second"]


@pytest.mark.asyncio
async def test_unexpected_close_does_not_terminate_transcript_iterator() -> None:
    ws1 = _FakeGladiaWS([_transcript_msg("only", is_final=True)])
    fake = _FakeGladia([ws1, _FakeGladiaWS()])
    backend = _backend(fake)
    await backend.push_audio(b"\x00\x00" * 320, 16000)
    task1 = backend._session_task  # noqa: SLF001
    assert task1 is not None
    ws1.force_server_close()
    await asyncio.wait_for(task1, timeout=1.0)

    # The transcript "only" is available, but the iterator has NOT been
    # sentinel-terminated — draining it would block. Read exactly one, then
    # close to unblock.
    it = backend.transcripts()
    first = await asyncio.wait_for(it.__anext__(), timeout=1.0)
    assert first.text == "only"
    await backend.close()


# ---------- status + error-mapping helpers (unit) --------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_raise_for_gladia_status_auth(status: int) -> None:
    with pytest.raises(STTAuthenticationError):
        _raise_for_gladia_status(status, model="solaria-1")


def test_raise_for_gladia_status_rate_limit() -> None:
    with pytest.raises(STTRateLimitError):
        _raise_for_gladia_status(429, model="solaria-1")


@pytest.mark.parametrize("status", [400, 500, 503])
def test_raise_for_gladia_status_other_failure(status: int) -> None:
    with pytest.raises(STTStreamFailureError):
        _raise_for_gladia_status(status, model="solaria-1")


def test_raise_for_gladia_status_ok_is_noop() -> None:
    _raise_for_gladia_status(200, model="solaria-1")  # does not raise
    _raise_for_gladia_status(201, model="solaria-1")


def test_raise_mapped_gladia_error_passes_domain_through() -> None:
    original = STTRateLimitError("rl", context={"provider": "gladia"})
    with pytest.raises(STTRateLimitError) as exc_info:
        _raise_mapped_gladia_error(original, model="solaria-1")
    assert exc_info.value is original


def test_raise_mapped_gladia_error_wraps_generic_as_stream_failure() -> None:
    with pytest.raises(STTStreamFailureError) as exc_info:
        _raise_mapped_gladia_error(ValueError("weird"), model="solaria-1")
    assert exc_info.value.context["provider"] == "gladia"
    assert isinstance(exc_info.value, STTError)
