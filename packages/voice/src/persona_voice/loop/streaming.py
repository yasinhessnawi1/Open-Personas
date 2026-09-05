"""Streaming-loop skeleton — the four named Protocol seams + the pass-through echo default.

D-V1-X-loop-skeleton-shape locks the async-generator pattern + dual-priority
queue (Pipecat InterruptionFrame discipline; R-V1-5 finding #5). Each seam
is a :class:`typing.Protocol` so V2 / V3 / V4 / V5 implementing agents can
satisfy it with provider-specific adapters without a base class. V1 owns the
plumbing; the seams stay provider-independent (R-V1-5 lean — "adopt the
seam shape, reject the SDK coupling").

The seams:

* **V2 — :class:`STTStream`**: push model. ``push_audio(pcm, sample_rate)``
  ingests inbound frames; ``transcripts()`` yields :class:`Transcript`
  records (``is_final``, ``text``, ``confidence``, ``eou_at``).
* **V3 — :class:`TTSStream`**: ``synthesize(text_stream)`` consumes an
  ``AsyncIterator[str]`` (LLM token stream) and yields
  :class:`AudioChunk` records. ``cancel()`` aborts in-flight synthesis
  (barge-in path — T08 binary criterion).
* **V4 — :class:`SessionEventListener`** (defined in
  :mod:`persona_voice.session.state_machine`): dual-priority queue dispatches
  ``UserStartedSpeaking`` / ``Interruption`` / ``EndOfTurn`` events on the
  SYSTEM priority lane so they bypass the audio data lane.
* **V5 — :class:`ModelReplyProducer`**: an async-callable producing
  ``AsyncIterator[str]`` tokens from a final-transcript trigger; never a
  blocking ``complete()`` call. The streaming-everywhere discipline
  (R-V1-3) hinges on V5 emitting tokens incrementally so V3 TTS can start
  audio before the LLM finishes — compresses additive latency into max().

The :class:`StreamingLoop` runs the streaming pipeline:

1. Inbound audio frames from :class:`VoiceRoom` arrive at
   :meth:`StreamingLoop._on_inbound_frame`.
2. If a :class:`STTStream` is wired, the frame is pushed into it; otherwise
   the loop falls back to :attr:`PassThroughEchoMode.ECHO` and pipes the
   inbound frame straight to the outbound rail (the pass-through default
   that lets T08 verify the full-duplex pipe before V2/V3/V5 wire).
3. Transcripts flow from V2 → V5; tokens flow from V5 → V3; audio chunks
   flow from V3 → :class:`VoiceRoom`'s outbound source.

Audio invariants (D-V1-6) are enforced at the seam boundary: inbound PCM16
mono 16 kHz; outbound PCM16 mono 24 kHz. Every audio-bearing record carries
its ``sample_rate`` explicitly so the sample-rate-mismatch bug R-V1-5 names
("the #1 production bug per AssemblyAI debugging guide") is structurally
impossible.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime  # noqa: TC003 — runtime for Pydantic field validation
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.errors import PersonaError
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict, Field

from persona_voice.session.state_machine import (
    SessionLifecycleEvent,
    SessionStateMachine,
)
from persona_voice.transport.room import (
    AUDIO_OUTBOUND_CHANNELS,
    AUDIO_OUTBOUND_SAMPLE_RATE,
    InboundAudioFrame,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona_voice.stt.protocol import SpeechActivityListener
    from persona_voice.transport.room import VoiceRoom


_LOG = get_logger("voice.streaming")

#: R9-124: the bound on a user turn's FIRST audio, measured from the turn-end decision.
DEFAULT_TURN_FIRST_AUDIO_TIMEOUT_S: float = 20.0
#: What the persona says when a turn cannot be answered (a stall, a provider failure or
#: a bug). Short, honest, and in the persona's own voice, so a failed turn is HEARD
#: instead of leaving the caller talking to silence.
TURN_FAILED_SPOKEN_LINE: str = "Sorry, I lost that for a moment. Could you say it again?"
#: Bound on speaking the line above; a TTS that is itself down must not re-hang the turn.
_FALLBACK_SPEECH_TIMEOUT_S: float = 8.0


async def _single_text(text: str) -> AsyncIterator[str]:
    """A one-item token stream, so a fixed line can ride the ordinary TTS path."""
    yield text


__all__ = [
    "AudioChunk",
    "HeardReply",
    "ModelReplyProducer",
    "PassThroughEchoMode",
    "ReplyHeardListener",
    "STTStream",
    "StreamingLoop",
    "TTSStream",
    "Transcript",
    "TurnOrchestrator",
    "VoiceCaptionListener",
]


# ---------- boundary records ------------------------------------------------


class Transcript(BaseModel):
    """V2 STT output — one (partial or final) recognised utterance.

    Frozen + ``extra="forbid"`` per the D-05-9 boundary discipline. The
    ``eou_at`` timestamp is what T10's VoiceLog samples for the
    end-of-utterance latency hop (R-V1-3 per-hop budget table).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    is_final: bool
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    eou_at: datetime | None = None
    #: ``True`` when this "utterance" is an internally-originated prompt riding
    #: the producer path — the turn-0 greeting nudge (Spec 32 A3) or a coalesced
    #: narration prompt — never speech from the caller. The persistence boundary
    #: reads this to keep internal prompts out of the user-facing transcript
    #: (R9-001): the turn's assistant reply still persists; no user row does.
    #: Structural by design, so the prompt wording is free to change without
    #: regressing (no text-matching anywhere).
    synthetic: bool = False


class AudioChunk(BaseModel):
    """V3 TTS output / V1 outbound rail — one PCM16 chunk + its sample rate.

    Always PCM16 mono. ``sample_rate`` is explicit per D-V1-6: every audio
    record carries the rate so resampling is structurally impossible inside
    the loop body (conversion happens at the seam adapter boundary).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    data: bytes
    sample_rate: int = Field(gt=0)
    num_channels: int = Field(default=1, gt=0)
    samples_per_channel: int = Field(gt=0)


# ---------- pass-through fallback ------------------------------------------


class PassThroughEchoMode(StrEnum):
    """Pass-through behaviour when no STT/TTS/Model seams are wired.

    ``ECHO`` — inbound audio frames are forwarded directly to the outbound
    rail (full-duplex proof for T08 binary criterion #3).
    ``DISABLED`` — inbound frames are dropped; outbound stays silent (the
    production default; the loop is only useful with at least V2+V3+V5
    wired).
    """

    ECHO = "echo"
    DISABLED = "disabled"


# ---------- V2/V3/V5 seam Protocols ----------------------------------------


@runtime_checkable
class STTStream(Protocol):
    """V2 — push-model streaming STT seam.

    ``push_audio`` ingests PCM16 16 kHz mono frames from the inbound rail;
    ``transcripts()`` yields :class:`Transcript` records (partials + final)
    as the recogniser produces them.
    """

    async def push_audio(self, pcm: bytes, sample_rate: int) -> None: ...

    def transcripts(self) -> AsyncIterator[Transcript]: ...


@runtime_checkable
class TTSStream(Protocol):
    """V3 — streaming TTS seam.

    ``synthesize`` consumes an ``AsyncIterator[str]`` (V5's LLM token
    stream) and yields :class:`AudioChunk` records. ``cancel`` aborts an
    in-flight synthesis — the barge-in path (V4).
    """

    def synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[AudioChunk]: ...

    async def cancel(self) -> None: ...


@runtime_checkable
class ModelReplyProducer(Protocol):
    """V5 — model-reply seam.

    Async-callable that consumes the final transcript and yields LLM tokens
    incrementally. The streaming contract is what compresses additive
    latency into max() (R-V1-3) — never a blocking ``complete()`` call.
    """

    async def __call__(self, final_transcript: Transcript) -> AsyncIterator[str]: ...


@runtime_checkable
class TurnOrchestrator(Protocol):
    """V4 — the turn-taking orchestrator seam the loop drives (spec V4 T05/T06).

    A consumer-defined Protocol (the same discipline V1 uses for ``STTStream`` /
    ``TTSStream``): the loop depends on this minimal surface, and V4's
    :class:`persona_voice.turn_taking.orchestrator.ConversationalOrchestrator`
    satisfies it structurally — so the loop never imports the orchestrator
    (no cycle; the orchestrator imports :class:`Transcript` from here).

    When a ``StreamingLoop`` is constructed with an ``orchestrator``, the
    V1 auto-invocation loop is **disabled** (it is the echo/dev baseline only;
    production always wires an orchestrator so no ungated auto-loop runs —
    D-V4-X-t05-orchestrator-default). The loop instead feeds the orchestrator
    each transcript and the model-first-audio / persona-finished signals; the
    orchestrator decides *when* to call back into the loop's
    :meth:`StreamingLoop.invoke_model_for_turn`.
    """

    async def on_transcript(self, transcript: Transcript) -> None: ...

    async def notify_model_first_audio(self) -> None: ...

    async def notify_persona_finished(self) -> None: ...

    async def notify_processing_yielded_no_audio(self) -> None: ...


class HeardReply(BaseModel):
    """What the persona actually said on one turn (spec V4 T07 — D-V4-4).

    The barged-over memory-honesty record. ``text`` is the reply text whose
    audio was streamed onto the outbound rail up to the point the turn ended
    (cleanly or via barge-in); ``truncated`` is ``True`` when the turn was cut
    short (barge-in / continuation) so the unspoken remainder was never
    synthesised. V5 consumes this to write episodic memory that reflects what
    was *heard*, not what was *planned* (spec V4 §8 honesty risk).

    Known limitation (carried into ``MAINTENANCE.md``): ``text`` is counted at
    the token→TTS boundary, so it can over-count by the buffered-but-unplayed
    tail that V3 flushes from the rail on barge-in. The refinement signal is
    the persona referencing content the user did not hear; the fix is
    playout-position tracking (V3 ``wait_for_playout``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    truncated: bool
    token_count: int = Field(ge=0)


@runtime_checkable
class ReplyHeardListener(Protocol):
    """Consumer-defined seam — notified once per turn with what was heard.

    V1 emits a :class:`HeardReply` at the end of every
    :meth:`StreamingLoop.invoke_model_for_turn` (clean or cancelled). V4's
    ``HeardWordsBridge`` adapts it onto V5's memory-write seam
    (``TurnTranscriptListener``). Implementations MUST NOT raise — this runs in
    the invocation's ``finally``, including during barge-in cancellation.
    """

    async def on_reply_heard(self, reply: HeardReply) -> None: ...


@runtime_checkable
class VoiceCaptionListener(Protocol):
    """Consumer-defined seam — the live caption/transcript broadcast (spec V6 A1).

    The V6 :class:`persona_voice.transport.broadcast.DataChannelBroadcaster`
    implements this to push partial/final captions to the browser over the data
    channel (D-V6-2 dual-region captions; D-V6-E2 same envelope as state). User
    captions stream from V2 transcripts; persona captions stream verbatim from
    the V5 reply tokens (the TTS source text — perfect, not ASR). Additive port,
    default ``None`` (V1/V2/V5 behaviour unchanged when unwired). Implementations
    MUST NOT raise — a caption broadcast runs on the live turn path.
    """

    async def on_user_transcript(self, transcript: Transcript) -> None: ...

    async def on_persona_text(self, text: str, *, is_final: bool) -> None: ...


# ---------- the streaming loop ---------------------------------------------


class StreamingLoop:
    """Wires :class:`VoiceRoom` to the V2/V3/V4/V5 seams.

    At v0.1 the loop ships:

    * Inbound audio dispatch (frame → V2 push OR echo).
    * V2 → V5 → V3 token-stream wiring (the streaming pipeline).
    * V4 lifecycle event dispatch (delegates to :class:`SessionStateMachine`
      which is the single source-of-truth for the User/Agent two-state
      machine).
    * Pass-through echo fallback so T08 can verify criterion #3
      independently of any STT/TTS/LLM adapter.

    The audio invariants (D-V1-6) are enforced at the seam boundary; the
    loop body never resamples.
    """

    def __init__(
        self,
        *,
        voice_room: VoiceRoom,
        session: SessionStateMachine,
        stt: STTStream | None = None,
        tts: TTSStream | None = None,
        model: ModelReplyProducer | None = None,
        echo_mode: PassThroughEchoMode = PassThroughEchoMode.ECHO,
        speech_activity: SpeechActivityListener | None = None,
        orchestrator: TurnOrchestrator | None = None,
        turn_transcript_listener: ReplyHeardListener | None = None,
        caption_listener: VoiceCaptionListener | None = None,
        first_audio_timeout_s: float = DEFAULT_TURN_FIRST_AUDIO_TIMEOUT_S,
    ) -> None:
        # Spec V2 D-V2-X-streaming-loop-additivity-shape — ADDITIVE
        # ``speech_activity`` injected port; backwards-compatible default
        # ``None``. The seam adapter (T06) merges Silero VAD + provider
        # endpointing events and dispatches to this listener; V4 (future)
        # is the listener. Pipecat issue #1323 production-bug precedent:
        # keeping activity events on a separate Protocol from transcripts
        # avoids the frame-reordering class of bugs.
        self._voice_room = voice_room
        self._session = session
        self._stt = stt
        self._tts = tts
        self._model = model
        self._echo_mode = echo_mode
        self._speech_activity = speech_activity
        # Spec V4 D-V4-X-t05-orchestrator-default — ADDITIVE orchestrator port.
        # When wired (production), the auto-invocation loop is disabled and V4
        # drives invocation explicitly via invoke_model_for_turn; default None
        # preserves V1's echo/dev auto-invoke baseline so every V1/V2/V3 test
        # stays green.
        self._orchestrator = orchestrator
        # Spec V4 T07 — ADDITIVE barged-over memory-honesty port. Emits a
        # HeardReply per turn (what was actually spoken); None preserves V1.
        self._reply_listener = turn_transcript_listener
        # Spec V6 A1 — ADDITIVE live-caption broadcast port. Forwards each V2
        # user transcript (partial+final) + the V5 persona reply text (streamed
        # verbatim) to the data-channel broadcaster; None preserves V1/V2/V5.
        self._caption_listener = caption_listener
        self._first_audio_timeout_s = first_audio_timeout_s
        self._pipeline_task: asyncio.Task[None] | None = None
        # V1 wires the inbound dispatcher into the VoiceRoom at construction
        # so frames that arrive during connect are not dropped on the floor.
        voice_room.set_inbound_handler(self._on_inbound_frame)

    @property
    def speech_activity(self) -> SpeechActivityListener | None:
        """V2 additive — the registered ``SpeechActivityListener``, if any.

        Production composition wires the V4 listener once at construction
        OR via this property's setter; downstream consumers (e.g.
        observability harnesses, integration tests) read via the property.
        """
        return self._speech_activity

    @speech_activity.setter
    def speech_activity(self, value: SpeechActivityListener | None) -> None:
        self._speech_activity = value

    @property
    def orchestrator(self) -> TurnOrchestrator | None:
        """V4 additive — the registered :class:`TurnOrchestrator`, if any.

        The composition root (T06 ``wire_orchestrated_loop``) sets this after
        construction so the loop↔orchestrator pair can be built without a
        chicken-and-egg (the orchestrator's actions are loop-backed). When set,
        :meth:`start_pipeline` drains transcripts into the orchestrator instead
        of auto-invoking the model.
        """
        return self._orchestrator

    @orchestrator.setter
    def orchestrator(self, value: TurnOrchestrator | None) -> None:
        self._orchestrator = value

    @property
    def turn_transcript_listener(self) -> ReplyHeardListener | None:
        """V4 T07 additive — the per-turn :class:`HeardReply` sink, if any."""
        return self._reply_listener

    @turn_transcript_listener.setter
    def turn_transcript_listener(self, value: ReplyHeardListener | None) -> None:
        self._reply_listener = value

    @property
    def caption_listener(self) -> VoiceCaptionListener | None:
        """V6 A1 additive — the live-caption broadcast sink, if any.

        The composition root (A0 ``build_agent_session``) sets this to the
        :class:`DataChannelBroadcaster` after construction so user transcripts +
        persona reply text are pushed to the browser over the data channel.
        """
        return self._caption_listener

    @caption_listener.setter
    def caption_listener(self, value: VoiceCaptionListener | None) -> None:
        self._caption_listener = value

    # ----- inbound + echo --------------------------------------------

    async def _on_inbound_frame(self, frame: InboundAudioFrame) -> None:
        """Single dispatch point for inbound PCM16/16k frames."""
        if self._stt is not None:
            await self._stt.push_audio(frame.data, frame.sample_rate)
            return
        if self._echo_mode == PassThroughEchoMode.ECHO:
            await self._echo_inbound_to_outbound(frame)

    async def _echo_inbound_to_outbound(self, frame: InboundAudioFrame) -> None:
        """Pipe one inbound frame straight to the outbound rail.

        The outbound rail is PCM16 mono 24 kHz; the inbound rail is PCM16
        mono 16 kHz. T08 echoes at the **inbound** rate to keep the loop
        body resample-free — the test asserts round-trip audio integrity,
        not a particular outbound rate, so this is a valid v0.1
        pass-through proof. T07 leaves sample-rate conversion to V3 adapter
        consumers.
        """
        # Lazy import — only needed if echo path actually fires, and keeps
        # the loop module import-light for tests that never touch outbound.
        from livekit import rtc

        await self._ensure_outbound_published()
        await self._voice_room.capture_outbound_frame(
            rtc.AudioFrame(
                data=frame.data,
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
                samples_per_channel=frame.samples_per_channel,
            )
        )

    async def _ensure_outbound_published(self) -> None:
        # ``publish_outbound`` is idempotent — calling on each echo path
        # invocation costs ~zero after the first call.
        await self._voice_room.publish_outbound()

    # ----- V2/V5/V3 pipeline -----------------------------------------

    async def start_pipeline(self) -> None:
        """Spawn the V2 → V5 → V3 streaming pipeline as a background task.

        Idempotent — calling twice is a no-op (the existing task continues).
        The pipeline reads transcripts from V2, hands the final transcript
        to V5, pipes V5's token stream into V3, and pushes V3's audio
        chunks into the outbound rail.
        """
        if self._pipeline_task is not None and not self._pipeline_task.done():
            return
        if self._orchestrator is not None:
            # Orchestrator-driven (production): V4 invokes the model per its
            # turn-end decision via invoke_model_for_turn. The loop only drains
            # transcripts into the orchestrator; it never auto-invokes the model
            # (D-V4-X-t05-orchestrator-default). Activity events reach the
            # orchestrator via the ``speech_activity`` port (T06 wiring).
            if self._stt is not None:
                self._pipeline_task = asyncio.create_task(
                    self._run_orchestrated_pipeline(self._stt, self._orchestrator),
                    name="voice-orchestrated-pipeline",
                )
            return
        if self._stt is None or self._model is None or self._tts is None:
            # V2/V3/V5 not all wired — pipeline can't run. Echo mode keeps
            # the room duplex for T08; intelligence stays pending.
            return
        self._pipeline_task = asyncio.create_task(
            self._run_pipeline(self._stt),
            name="voice-streaming-pipeline",
        )

    async def _run_orchestrated_pipeline(
        self, stt: STTStream, orchestrator: TurnOrchestrator
    ) -> None:
        """Feed V2 transcripts to V4 (orchestrator mode) — never auto-invoke.

        The orchestrator decides *when* to invoke the model (on its turn-end
        decision) via :meth:`invoke_model_for_turn`; the loop's only job here is
        to deliver each transcript so the endpointing policy has the text it
        needs (the textual-completion gate, D-V4-1).
        """
        async for transcript in stt.transcripts():
            await orchestrator.on_transcript(transcript)
            # V6 A1 — stream the user's caption (partial+final) to the browser.
            if self._caption_listener is not None:
                await self._caption_listener.on_user_transcript(transcript)

    async def _run_pipeline(self, stt: STTStream) -> None:
        """Drive the V2 → V5 → V3 streaming chain (the V1 auto-invoke baseline).

        For each *final* transcript V2 emits, the loop invokes the model and
        streams the reply (:meth:`invoke_model_for_turn`). This path runs ONLY
        when no orchestrator is wired (echo/dev baseline); production routes
        invocation through V4 (D-V4-X-t05-orchestrator-default).
        """
        await self._ensure_outbound_published()
        async for transcript in stt.transcripts():
            if not transcript.is_final:
                continue
            await self.invoke_model_for_turn(transcript)

    async def invoke_model_for_turn(self, final_transcript: Transcript) -> None:
        """Invoke V5 for one completed user turn and stream the reply to V3.

        The single-turn invocation V4 calls explicitly on its ``TURN_ENDED``
        decision (and the V1 auto-loop calls per final transcript). Hands the
        transcript to the V5 model, pipes the token stream into V3 synthesis,
        and pushes each audio chunk onto the outbound rail. A no-op if V5 or V3
        is not wired. The ``AGENT_STARTED_SPEAKING`` / ``AGENT_STOPPED_SPEAKING``
        lifecycle events bracket the invocation (the V6/audit hooks).

        Cancellation: V4 cancels the task awaiting this coroutine on barge-in;
        the ``CancelledError`` propagates into V3's ``synthesize`` iterator
        (which ends exception-free via its sentinel) and into the V5 backend's
        ``async with`` stream (clean connection close) — the three-things-
        stopping-together (spec V4 §8). The ``finally`` always emits
        ``AGENT_STOPPED_SPEAKING``.
        """
        if self._model is None or self._tts is None:
            return
        await self._ensure_outbound_published()
        await self._session.notify(SessionLifecycleEvent.AGENT_STARTED_SPEAKING)
        produced_audio = False
        completed = False
        heard: list[str] = []
        started = time.perf_counter()
        first_audio_ms: float | None = None
        outcome = "completed"
        _LOG.info(
            "voice turn started chars={chars} synthetic={synthetic}",
            chars=len(final_transcript.text),
            synthetic=final_transcript.synthetic,
        )
        try:
            # R9-124: everything up to the FIRST audio chunk runs under one deadline. A
            # hung provider used to hold the turn for the full request timeout, twice,
            # in silence; the deadline turns that into a logged, spoken, bounded failure.
            async with asyncio.timeout(self._first_audio_timeout_s):
                token_stream = await self._model(final_transcript)
                source = self._accumulate_heard(token_stream, heard)
                # V6 A1: tee the persona reply text to the caption broadcast as it
                # streams (verbatim from the TTS source, per D-V6-2). The final caption
                # is emitted in ``finally`` so it fires on the barge-in path too
                # (heard = spoken-so-far prefix).
                if self._caption_listener is not None:
                    source = self._tee_persona_captions(source, heard)
                audio = self._tts.synthesize(source).__aiter__()
                first_chunk = await anext(audio, None)
            if first_chunk is not None:
                await self._push_audio_chunk(first_chunk)
                produced_audio = True
                first_audio_ms = (time.perf_counter() - started) * 1000.0
                # First persona audio on the rail: PROCESSING to PERSONA_SPEAKING.
                if self._orchestrator is not None:
                    await self._orchestrator.notify_model_first_audio()
                async for chunk in audio:
                    await self._push_audio_chunk(chunk)
            # Clean completion (not cancelled): tell V4 the turn is over so it
            # returns the floor. An empty reply (no audio) resets to LISTENING.
            completed = True
            if not produced_audio:
                outcome = "no_audio"
            await self._return_floor(produced_audio)
        except TimeoutError:
            # The deadline expired before any audio: the stall the owner heard as
            # "stuck on listening". asyncio.timeout already unwound the stream; now
            # make the failure audible and give the floor back.
            outcome = "stalled"
            _LOG.warning(
                "voice turn stalled: no first audio within {timeout_s}s (chars={chars}); "
                "cancelled, speaking the fallback line and returning the floor",
                timeout_s=self._first_audio_timeout_s,
                chars=len(final_transcript.text),
            )
            produced_audio = await self._speak_turn_failed(already_speaking=produced_audio)
            await self._return_floor(produced_audio)
        except PersonaError as exc:
            # A model/TTS PROVIDER failure (e.g. Cartesia 402 "quota_exceeded" —
            # out of credits — mapped to TTSStreamFailureError, or a network drop)
            # must NOT surface as an unretrieved task exception that leaves the
            # call wedged. The persona simply can't speak this turn: log it
            # actionably and degrade so V4 returns the floor (turn-0 already
            # degraded via the greet timeout). Scoped to the domain PersonaError
            # hierarchy so structural-invariant bugs (e.g. the D-V1-6 sample-rate
            # ValueError) still raise loudly, and ``CancelledError`` (a
            # BaseException) still unwinds the stream on barge-in (spec V4 §8).
            outcome = "provider_error"
            _LOG.warning(
                "voice turn failed at the provider (class={cls}); speaking the fallback "
                "line and returning the floor: {err}",
                cls=type(exc).__name__,
                err=repr(exc)[:300],
            )
            produced_audio = await self._speak_turn_failed(already_speaking=produced_audio)
            await self._return_floor(produced_audio)
        except Exception as exc:  # noqa: BLE001 — a bug must not leave the caller in silence
            # A non-domain exception is a bug and is logged as one, loudly, with its
            # traceback. It is NOT re-raised: this coroutine runs as a task nobody
            # awaits, so a re-raise is exactly the silent failure this handler ends.
            # The floor comes back and the caller hears that the turn failed.
            outcome = "bug"
            _LOG.exception(
                "voice turn crashed (class={cls}); speaking the fallback line and "
                "returning the floor",
                cls=type(exc).__name__,
            )
            produced_audio = await self._speak_turn_failed(already_speaking=produced_audio)
            await self._return_floor(produced_audio)
        finally:
            _LOG.info(
                "voice turn ended outcome={outcome} tokens={tokens} "
                "first_audio_ms={first} total_ms={total:.0f}",
                outcome=outcome,
                tokens=len(heard),
                first=None if first_audio_ms is None else round(first_audio_ms),
                total=(time.perf_counter() - started) * 1000.0,
            )
            await self._session.notify(SessionLifecycleEvent.AGENT_STOPPED_SPEAKING)
            # T07 barged-over memory honesty (D-V4-4): emit what was actually
            # heard — the full reply on clean completion, the spoken-so-far
            # prefix when cut short by barge-in/continuation (``completed`` is
            # False if CancelledError unwound the stream). Runs in finally so it
            # fires on both paths.
            if self._reply_listener is not None:
                await self._reply_listener.on_reply_heard(
                    HeardReply(
                        text="".join(heard),
                        truncated=not completed,
                        token_count=len(heard),
                    )
                )
            # V6 A1 — the persona's FINAL caption (D-V6-2): the full reply on
            # clean completion, the spoken-so-far prefix on barge-in. Fires on
            # both paths (in finally) so the client's persona caption settles to
            # a final even when the turn was cut short.
            if self._caption_listener is not None:
                await self._caption_listener.on_persona_text("".join(heard), is_final=True)

    async def _speak_turn_failed(self, *, already_speaking: bool) -> bool:
        """Say :data:`TURN_FAILED_SPOKEN_LINE` through V3; return whether audio hit the rail.

        Best effort and bounded: a TTS that is itself down must not re-hang the turn, so
        any failure here is logged and the caller returns a silent floor instead.

        Args:
            already_speaking: Whether this turn already put audio on the rail, so the
                first-audio notification is not sent twice.

        Returns:
            ``True`` if any audio reached the rail during this turn.
        """
        if self._tts is None:
            return already_speaking
        spoke = already_speaking
        try:
            async with asyncio.timeout(_FALLBACK_SPEECH_TIMEOUT_S):
                async for chunk in self._tts.synthesize(_single_text(TURN_FAILED_SPOKEN_LINE)):
                    await self._push_audio_chunk(chunk)
                    if not spoke:
                        spoke = True
                        if self._orchestrator is not None:
                            await self._orchestrator.notify_model_first_audio()
        except Exception as exc:  # noqa: BLE001 — the fallback must never raise
            _LOG.warning("voice fallback line could not be spoken: {err}", err=repr(exc)[:200])
        return spoke

    async def _return_floor(self, produced_audio: bool) -> None:
        """Hand the floor back to V4 after a turn ends, spoken or silent."""
        if self._orchestrator is None:
            return
        if produced_audio:
            await self._orchestrator.notify_persona_finished()
        else:
            await self._orchestrator.notify_processing_yielded_no_audio()

    @staticmethod
    async def _accumulate_heard(
        token_stream: AsyncIterator[str], sink: list[str]
    ) -> AsyncIterator[str]:
        """Tee the V5 token stream into ``sink`` as each token passes to V3.

        The heard-words counter (D-V4-X-heard-words-counter): tokens are
        recorded at the token→TTS boundary. On barge-in the iteration is
        cancelled mid-stream, so ``sink`` holds exactly the prefix delivered for
        synthesis — over-counting only by the buffered-but-unplayed tail V3
        flushes (the documented MAINTENANCE.md limitation).
        """
        async for token in token_stream:
            sink.append(token)
            yield token

    async def _tee_persona_captions(
        self, source: AsyncIterator[str], heard: list[str]
    ) -> AsyncIterator[str]:
        """Emit a running persona caption per token while passing tokens through.

        ``source`` is :meth:`_accumulate_heard`, which appends each token to
        ``heard`` BEFORE yielding it — so ``"".join(heard)`` is the running reply
        text at the point the token reaches this tee. Each emit is a partial
        (``is_final=False``); the client mutate-and-replaces the current persona
        segment (D-V6-2 anti-flicker). The final is emitted in
        :meth:`invoke_model_for_turn`'s ``finally``. Only runs when a caption
        listener is wired, so the per-token broadcast costs nothing otherwise.
        """
        async for token in source:
            # heard already includes this token (appended by _accumulate_heard).
            await self._caption_listener.on_persona_text(  # type: ignore[union-attr]
                "".join(heard), is_final=False
            )
            yield token

    async def _push_audio_chunk(self, chunk: AudioChunk) -> None:
        """Push one V3 :class:`AudioChunk` onto the outbound rail.

        Validates the sample rate matches the outbound rail (D-V1-6: PCM16
        mono 24 kHz). Mismatch fails loud — the V3 adapter owns rate
        conversion at its boundary, not the loop.
        """
        if chunk.sample_rate != AUDIO_OUTBOUND_SAMPLE_RATE:
            msg = (
                f"outbound rail expects {AUDIO_OUTBOUND_SAMPLE_RATE} Hz; "
                f"V3 TTSStream produced {chunk.sample_rate} Hz"
            )
            raise ValueError(msg)
        if chunk.num_channels != AUDIO_OUTBOUND_CHANNELS:
            msg = (
                f"outbound rail expects {AUDIO_OUTBOUND_CHANNELS} channel(s); "
                f"V3 TTSStream produced {chunk.num_channels}"
            )
            raise ValueError(msg)
        from livekit import rtc

        await self._voice_room.capture_outbound_frame(
            rtc.AudioFrame(
                data=chunk.data,
                sample_rate=chunk.sample_rate,
                num_channels=chunk.num_channels,
                samples_per_channel=chunk.samples_per_channel,
            )
        )

    # ----- barge-in / lifecycle --------------------------------------

    async def interrupt(self) -> None:
        """V4 barge-in entry — cancel in-flight TTS + flush + notify session.

        Called by V4 (out of scope for V1) when the user starts speaking
        while the agent is mid-utterance. The TTS cancel ends the
        ``synthesize`` AsyncIterator via sentinel (Spec V3 steps 1-3); the
        outbound-queue clear drops already-queued frames so the rail goes
        quiet near-immediately rather than draining the jitter buffer (Spec
        V3 D-V3-5 step 4 — without it R-V3-5's "ghost audio" plays on).
        """
        await self.flush_outbound_and_cancel_tts()
        await self._session.notify(SessionLifecycleEvent.AGENT_STOPPED_SPEAKING)

    async def flush_outbound_and_cancel_tts(self) -> None:
        """The teardown half of barge-in — cancel TTS + clear the rail, no notify.

        Shared by V1's :meth:`interrupt` (which adds the ``AGENT_STOPPED_SPEAKING``
        notify) and V4's orchestrated barge-in (T06's ``LoopTurnActions.interrupt``,
        where the cancelled model-invocation task's ``finally`` already emits the
        single ``AGENT_STOPPED_SPEAKING`` — so this half must NOT notify again,
        avoiding a duplicate lifecycle event).
        """
        if self._tts is not None:
            await self._tts.cancel()
        self._voice_room.clear_outbound()

    async def stop(self) -> None:
        """Stop the pipeline task cleanly (called by session teardown)."""
        if self._pipeline_task is not None and not self._pipeline_task.done():
            self._pipeline_task.cancel()
        self._pipeline_task = None
