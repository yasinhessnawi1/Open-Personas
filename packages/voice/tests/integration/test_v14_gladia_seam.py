"""Spec V14 T1 — real-chain seam composition for the Gladia streaming backend.

Proves the REAL :class:`GladiaStreamingSTT` (with injected scripted transports —
zero live spend) composes behind the REAL
:class:`persona_voice.stt.seam_adapter.V1STTStreamSeamAdapter` + the Spec V8
cost :class:`~persona_voice.stt.protocol.StreamGate`, and that the T0
idle-finding mitigation works end-to-end:

* transcripts flow through the seam's ``transcripts()`` port, and
* when the V8 gate CLOSES for a long persona turn and the Gladia session
  idle-closes during that gap, the next gate-reopen frame transparently
  RECONNECTS a fresh Gladia session (rides the seam's ring-buffer-on-reopen) so
  no transcript is lost.

The Silero VAD is not under test here (T1 is the backend leg), so a minimal fake
VAD keeps the composition hermetic + fast; the real VAD path is covered by the
existing ``test_v2_streaming_stt.py`` spine. No ONNX, no network, no DB.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from persona_voice.stt.config import StreamingSTTConfig
from persona_voice.stt.gladia_backend import GladiaStreamingSTT
from persona_voice.stt.seam_adapter import V1STTStreamSeamAdapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent

pytestmark = pytest.mark.integration

_FRAME = b"\x01\x02" * 320  # 640 bytes = 20ms of PCM16 mono @ 16 kHz


def _transcript_msg(text: str, *, is_final: bool) -> str:
    return json.dumps(
        {"type": "transcript", "data": {"is_final": is_final, "utterance": {"text": text}}}
    )


# ---------- fakes (transport + VAD + gate) ---------------------------------


class _FakeGladiaWS:
    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self.sent: list[bytes | str] = []
        self._close_evt = asyncio.Event()

    async def __aenter__(self) -> _FakeGladiaWS:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def send(self, data: bytes | str) -> None:
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
        self._close_evt.set()


class _FakeGladia:
    def __init__(self, sessions: list[_FakeGladiaWS]) -> None:
        self._sessions = list(sessions)
        self.open_calls = 0

    async def open_session(self) -> tuple[str, str]:
        self.open_calls += 1
        return (f"sid-{self.open_calls}", f"wss://fake/{self.open_calls}")

    def ws_connect(self, _url: str) -> _FakeGladiaWS:
        return self._sessions.pop(0)


class _FakeVAD:
    """Minimal Silero-VAD stand-in — records frames, emits no activity."""

    def __init__(self) -> None:
        self.pushed: list[tuple[bytes, int]] = []

    async def load(self) -> None: ...

    async def push_audio(self, pcm: bytes, sample_rate: int) -> None:
        self.pushed.append((pcm, sample_rate))

    async def _events(self) -> AsyncIterator[SpeechStartedEvent | SpeechEndedEvent]:
        return
        yield  # pragma: no cover

    def speech_activity_events(self) -> AsyncIterator[SpeechStartedEvent | SpeechEndedEvent]:
        return self._events()

    async def close(self) -> None: ...


class _ToggleGate:
    """A controllable Spec V8 StreamGate: ``is_open`` reflects a mutable flag."""

    def __init__(self, *, is_open: bool = True) -> None:
        self._open = is_open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def is_open(self) -> bool:
        return self._open


def _gladia_backend(fake: _FakeGladia) -> GladiaStreamingSTT:
    return GladiaStreamingSTT(
        StreamingSTTConfig(provider="gladia", gladia_api_key="gl-secret"),
        open_session=fake.open_session,
        ws_connect=fake.ws_connect,  # type: ignore[arg-type]
    )


# ---------- tests -----------------------------------------------------------


@pytest.mark.asyncio
async def test_gladia_streams_transcripts_through_the_seam() -> None:
    """The real seam adapter forwards the Gladia backend's transcript stream;
    a frame pushed through the seam reaches the backend socket."""
    ws = _FakeGladiaWS(
        [_transcript_msg("hei", is_final=False), _transcript_msg("hei der", is_final=True)]
    )
    fake = _FakeGladia([ws])
    backend = _gladia_backend(fake)
    vad = _FakeVAD()
    seam = V1STTStreamSeamAdapter(backend=backend, vad=vad)  # type: ignore[arg-type]

    await seam.push_audio(_FRAME, 16000)
    await seam.close()

    collected = [t async for t in seam.transcripts()]
    assert [t.text for t in collected] == ["hei", "hei der"]
    assert vad.pushed  # the VAD always receives the frame (split-tee)
    assert _FRAME in ws.sent  # the backend socket received the frame


@pytest.mark.asyncio
async def test_gladia_reconnects_after_idle_close_during_a_gated_window() -> None:
    """The T0 idle mitigation, end-to-end through the V8 gate + seam ring-buffer.

    Session 1 streams while the gate is open. The gate then CLOSES (persona
    speaking) and the Gladia session idle-closes during the gap; the seam
    withholds frames from the billed backend (ring-buffering them). On gate
    REOPEN the seam flushes the ring to the backend, which transparently opens a
    FRESH session — and the post-reopen transcript still arrives.
    """
    ws1 = _FakeGladiaWS([_transcript_msg("before the gap", is_final=True)])
    ws2 = _FakeGladiaWS([_transcript_msg("after the reopen", is_final=True)])
    fake = _FakeGladia([ws1, ws2])
    backend = _gladia_backend(fake)
    gate = _ToggleGate(is_open=True)
    seam = V1STTStreamSeamAdapter(
        backend=backend,
        vad=_FakeVAD(),  # type: ignore[arg-type]
        gate=gate,
        reopen_preroll_ms=300.0,
    )

    # --- open window: session 1 opens + streams ---
    await seam.push_audio(_FRAME, 16000)
    session1_task = backend._session_task  # noqa: SLF001 — white-box lifecycle assertion
    assert session1_task is not None
    assert fake.open_calls == 1

    # --- gate closes (persona speaking); the session idle-closes in the gap ---
    gate.close()
    await seam.push_audio(_FRAME, 16000)  # withheld from the backend, ring-buffered
    ws1.force_server_close()
    await asyncio.wait_for(session1_task, timeout=1.0)
    assert fake.open_calls == 1  # no reconnect yet — nothing pushed to the backend

    # --- gate reopens: ring flush + live frame reconnect a fresh session ---
    gate.open()
    await seam.push_audio(_FRAME, 16000)
    assert fake.open_calls == 2  # transparent reconnect on the reopen frame

    await seam.close()
    collected = [t async for t in seam.transcripts()]
    assert [t.text for t in collected] == ["before the gap", "after the reopen"]
    # Cost accounting excluded the gated frame from the billed stream, but the
    # ring flush + open-window frames are counted (D-V8-X-cost-rebase).
    assert seam.streamed_seconds > 0.0
