"""Spec V2 T11 integration spine — scripted-provider fixture in-process.

Covers the Spec V2 acceptance criteria that are provable without a live
provider:

* **Criterion #2 BINARY** — partials emit BEFORE utterance-end. The
  scripted backend yields multiple partials (``is_final=False``) before
  the FINAL (``is_final=True``) so the integration proves the
  streaming-everywhere shape (R-V2-4 LiveKit / Pipecat convergent SEAM).
* **Criterion #3** — ``speech_started`` + ``speech_ended`` events fire on
  the V4 ``SpeechActivityListener`` port (T07 additive shape).
* **Criterion #5** — V1's :class:`VoiceLog` accepts the four T08 additive
  STT fields populated by V2 (round-trip + ge=0 constraint sanity).
* **Criterion #6** — V1's :class:`StreamingLoop` is wired with stt = V2
  seam adapter + speech_activity = V4 listener. ECHO mode is OFF (no
  inbound→outbound pass-through).
* **Criterion #9** — sensor-vs-decider holds: V2 emits events but DOES
  NOT decide turn-taking (no turn_over / commit_turn calls; V4 owns).
* **D-V2-X-echo-cancellation-v1-dependency mitigation** — the TTS-mute
  window suppresses ``speech_started`` while the persona is speaking.

The integration runs entirely in-process: no LiveKit Server (T08 V1 already
proved full-duplex); no real Deepgram (T12 external smoke is the operator-
pass gate for live STT). The MockStreamingSTT fixture lives at
``packages/voice/tests/_mock_stt_backend.py`` per Spec 02 D-02-12.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from persona_voice.loop.streaming import (
    PassThroughEchoMode,
    StreamingLoop,
    Transcript,
)
from persona_voice.session.state_machine import SessionStateMachine
from persona_voice.stt.config import StreamingSTTConfig
from persona_voice.stt.seam_adapter import V1STTStreamSeamAdapter
from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent
from persona_voice.stt.vad_silero import (
    SILERO_SAMPLE_RATE_HZ,
    SileroVADAdapter,
)
from persona_voice.transport.room import InboundAudioFrame

# T04's MockStreamingSTT lives in packages/voice/tests/_mock_stt_backend.py per
# Spec 02 D-02-12 (mock backends live in tests/, NOT in production src/).
# The repo's root conftest only adds packages/*/src to sys.path, so we
# extend with the voice tests dir here for cross-test import.
_VOICE_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_VOICE_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_VOICE_TESTS_DIR))

from _mock_stt_backend import MockStreamingSTT  # type: ignore[import-not-found]  # noqa: E402
from _speech_audio import speech_pcm  # type: ignore[import-not-found]  # noqa: E402

pytestmark = pytest.mark.integration


# ---------- helpers + fixtures ----------------------------------------------


class _CapturingListener:
    """Captures speech_started + speech_ended events for assertions."""

    def __init__(self) -> None:
        self.started: list[SpeechStartedEvent] = []
        self.ended: list[SpeechEndedEvent] = []

    async def on_speech_started(self, event: SpeechStartedEvent) -> None:
        self.started.append(event)

    async def on_speech_ended(self, event: SpeechEndedEvent) -> None:
        self.ended.append(event)


def _build_voice_room_fake() -> Any:  # noqa: ANN401
    vr = MagicMock()
    vr.set_inbound_handler = MagicMock()
    vr.set_disconnect_handler = MagicMock()
    vr.publish_outbound = AsyncMock()
    vr.capture_outbound_frame = AsyncMock()
    return vr


def _build_session() -> SessionStateMachine:
    engine = MagicMock()
    engine.dispose = MagicMock()
    return SessionStateMachine(
        session_id="s-v2-t11",
        user_id="u-v2-t11",
        persona_id="p-v2-t11",
        conversation_id="c-v2-t11",
        rls_engine=engine,
    )


def _config() -> StreamingSTTConfig:
    return StreamingSTTConfig(
        provider="deepgram",
        model="nova-3",
        api_key="test-key",  # type: ignore[arg-type]
        vad_library="silero",
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=50,
        silero_min_silence_duration_ms=200,
    )


# ---------- Criterion #2 BINARY — partials before utterance-end ------------


@pytest.mark.asyncio
async def test_criterion_2_partials_emit_before_utterance_end() -> None:
    """Scripted provider yields 3 partials then a FINAL; the seam adapter's
    transcripts() iterator MUST surface partials in order BEFORE the final."""
    scripted = [
        Transcript(is_final=False, text="hel", confidence=0.55),
        Transcript(is_final=False, text="hello", confidence=0.78),
        Transcript(is_final=False, text="hello wor", confidence=0.85),
        Transcript(is_final=True, text="hello world", confidence=0.95),
    ]
    backend = MockStreamingSTT(scripted_transcripts=scripted)
    vad = SileroVADAdapter(_config())
    adapter = V1STTStreamSeamAdapter(backend=backend, vad=vad)
    await adapter.load()
    received: list[Transcript] = []
    async for transcript in adapter.transcripts():
        received.append(transcript)
    # Three partials BEFORE the final — streaming-everywhere proof.
    assert len(received) == 4
    assert [t.is_final for t in received] == [False, False, False, True]
    assert received[-1].text == "hello world"
    await adapter.close()


# ---------- Criterion #3 — speech_started + speech_ended on listener -------


@pytest.mark.asyncio
async def test_criterion_3_speech_activity_events_fire_on_listener() -> None:
    scripted_events = [
        SpeechStartedEvent(
            ts_audio_s=0.1,
            ts_emit=datetime.now(UTC),
            source="provider",
            synthetic=False,
            confidence=0.9,
        ),
        SpeechEndedEvent(
            ts_audio_s=2.0,
            ts_emit=datetime.now(UTC),
            source="provider",
            synthetic=False,
            transcript_settled=True,
            corroborates=False,
            silence_duration_ms=None,
        ),
    ]
    backend = MockStreamingSTT(
        scripted_transcripts=[Transcript(is_final=True, text="x", confidence=0.9)],
        scripted_activity_events=scripted_events,
    )
    vad = SileroVADAdapter(_config())
    listener = _CapturingListener()
    adapter = V1STTStreamSeamAdapter(backend=backend, vad=vad, listener=listener)
    await adapter.load()
    await adapter.push_audio(b"\x00\x00" * 256, SILERO_SAMPLE_RATE_HZ)
    # Give drainer tasks a tick to run.
    await asyncio.sleep(0.05)
    assert len(listener.started) >= 1
    assert listener.started[0].source == "provider"
    assert len(listener.ended) >= 1
    # Provider-sourced speech_ended is marked corroborates=True by the seam adapter.
    assert listener.ended[0].corroborates is True
    await adapter.close()


# ---------- Criterion #5 — VoiceLog accepts T08 additive STT fields -------


def test_criterion_5_voice_log_carries_t08_stt_fields() -> None:
    """T08's 4 additive fields round-trip through VoiceLog JSON cleanly."""
    from persona_voice.logging import VoiceLog

    ts = datetime.now(UTC)
    log = VoiceLog(
        session_id="s",
        user_id="u",
        persona_id="p",
        conversation_id="c",
        turn_index=0,
        started_at=ts,
        stt_partial_first_at=ts,
        stt_audio_pushed_at=ts,
        stt_provider_cost_cents_per_minute=0.48,  # Deepgram PAYG cents/min
        stt_total_cents=2.4,  # 5-min call
    )
    rehydrated = VoiceLog.model_validate_json(log.model_dump_json())
    assert rehydrated.stt_partial_first_at == ts
    assert rehydrated.stt_audio_pushed_at == ts
    assert rehydrated.stt_provider_cost_cents_per_minute == 0.48
    assert rehydrated.stt_total_cents == 2.4


# ---------- Criterion #6 — V1 StreamingLoop wired with V2 seam adapter ---


@pytest.mark.asyncio
async def test_criterion_6_v1_streaming_loop_with_v2_seam_no_echo() -> None:
    """StreamingLoop accepts the V1STTStreamSeamAdapter as the stt port +
    a listener on the speech_activity port; ECHO mode is OFF (the seam
    adapter does NOT call back into the outbound rail)."""
    backend = MockStreamingSTT(
        scripted_transcripts=[Transcript(is_final=True, text="hi", confidence=0.9)],
    )
    vad = SileroVADAdapter(_config())
    listener = _CapturingListener()
    adapter = V1STTStreamSeamAdapter(backend=backend, vad=vad, listener=listener)
    await adapter.load()
    vr = _build_voice_room_fake()
    sm = _build_session()
    loop = StreamingLoop(
        voice_room=vr,
        session=sm,
        stt=adapter,
        speech_activity=listener,
        echo_mode=PassThroughEchoMode.DISABLED,
    )
    # Push one frame through the loop's _on_inbound_frame dispatcher.
    frame = InboundAudioFrame(
        data=b"\x00\x00" * 256,
        sample_rate=SILERO_SAMPLE_RATE_HZ,
        num_channels=1,
        samples_per_channel=256,
    )
    await loop._on_inbound_frame(frame)  # noqa: SLF001
    # MockStreamingSTT recorded the push.
    assert backend.pushed == [(frame.data, SILERO_SAMPLE_RATE_HZ)]
    # ECHO disabled — the outbound rail was NOT touched.
    vr.capture_outbound_frame.assert_not_called()
    # The listener is wired through the StreamingLoop additive port.
    assert loop.speech_activity is listener
    await adapter.close()


# ---------- Criterion #9 — sensor-vs-decider boundary holds ---------------


def test_criterion_9_v2_does_not_make_turn_taking_decisions() -> None:
    """V2 is the SENSOR. Search V2 source for V4 decider keywords; should
    find none. This is a structural / negative assertion."""
    from pathlib import Path

    v2_stt_dir = Path("packages/voice/src/persona_voice/stt")
    forbidden_keywords = ["commit_turn", "turn_over", "decide_turn"]
    matches: list[tuple[Path, str]] = []
    for path in v2_stt_dir.rglob("*.py"):
        text = path.read_text()
        for keyword in forbidden_keywords:
            if keyword in text:
                matches.append((path, keyword))
    assert matches == [], (
        f"Sensor-vs-decider boundary violated (D-V2-2): V2 STT source contains "
        f"V4-decider keywords {matches!r}. V4 owns turn-taking; V2 only EMITS "
        f"speech_started/speech_ended events."
    )


# ---------- D-V2-X-echo-cancellation-v1-dependency TTS-mute-window mitigation


@pytest.mark.asyncio
async def test_d_v2_x_echo_cancellation_mute_window_suppresses_speech_started() -> None:
    """The session_state_provider returning True (agent speaking) suppresses
    listener-facing speech_started notifications — V0.1 backstop for the
    undocumented V1 AEC dependency surfaced by Phase-3 critic gate."""
    agent_speaking = {"speaking": True}

    def state_provider() -> bool:
        return agent_speaking["speaking"]

    config = StreamingSTTConfig(
        provider="deepgram",
        api_key="test-key",  # type: ignore[arg-type]
        vad_library="silero",
        silero_activation_threshold=0.5,
        silero_min_speech_duration_ms=32,
    )
    vad = SileroVADAdapter(config, session_state_provider=state_provider)
    await vad.load()
    # 300 ms of rendered speech (Cartesia TTS); a pure tone is not speech to the model.
    await vad.push_audio(speech_pcm(300), SILERO_SAMPLE_RATE_HZ)
    # The model did hear speech (else an empty queue would prove nothing) ...
    assert vad._in_speech  # noqa: SLF001
    # ... and the listener-facing event MUST NOT fire while agent is speaking.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(vad._event_queue.get(), timeout=0.05)  # noqa: SLF001
    await vad.close()
