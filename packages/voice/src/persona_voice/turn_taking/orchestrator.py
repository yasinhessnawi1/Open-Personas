"""ConversationalOrchestrator — the state-machine driver (spec V4 T04).

This is where the pure decision policies (T02 controller, T03 detector) and
the state vocabulary (T01) become a live conversation. The orchestrator:

* holds the current :class:`ConversationalState`;
* consumes V2's speech-activity signal (it IS a
  :class:`persona_voice.stt.protocol.SpeechActivityListener`) and V2's
  transcripts;
* runs the two judgement surfaces (turn-taking + barge-in) at the right
  moments and drives the guarded state transitions;
* performs the turn actions (invoke / cancel / interrupt) through an injected
  :class:`TurnActions` seam (T06 backs it with the V1 ``StreamingLoop``);
* broadcasts every transition on a :class:`ConversationalStateListener` seam
  (V6 renders it);
* owns the agent-speaking mute-window provider V2's VAD adapter consumes
  (D-V2-X-echo-cancellation) — :meth:`is_agent_speaking`.

**Timing is injected, not wall-clock-bound.** The two time-delayed decisions
— turn-end (after the silence threshold) and barge-in confirmation (after the
confirm window) — are scheduled through a :class:`Scheduler` seam and timed
with an injected ``clock``. The default scheduler wraps ``asyncio``; tests
inject a deterministic fake. This keeps the orchestrator's wiring exhaustively
unit-testable (the spec's whole point — the parameters are tuned, so the
machinery around them must be testable without real time).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger

from persona_voice.loop.streaming import Transcript
from persona_voice.turn_taking.barge_in import BargeInDetector, BargeInVerdict
from persona_voice.turn_taking.controller import TurnTakingController, TurnVerdict
from persona_voice.turn_taking.states import (
    AgentState,
    ConversationalState,
    ConversationalTransition,
    TransitionTrigger,
    advance,
    agent_state_for,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona_voice.stt.types import SpeechEndedEvent, SpeechStartedEvent

__all__ = [
    "AsyncioScheduler",
    "ConversationalOrchestrator",
    "ConversationalStateListener",
    "Scheduler",
    "SchedulerHandle",
    "TurnActions",
]

_logger = get_logger("turn_taking.orchestrator")

# Greet-first turn-0 bounds (Spec 32 A3). Defaults sit on the research degrade
# ladder (R-32-2); build_agent_session passes the env-configured values
# (D-32-X-degrade-timeout-env-config).
DEFAULT_WARMUP_TIMEOUT_S: float = 10.0
DEFAULT_GREET_TIMEOUT_S: float = 30.0
#: R9-119: how many times a PHANTOM turn (uncorroborated offset, no transcript) may
#: re-arm the turn-end timer before the floor is forced back to LISTENING. Small on
#: purpose: each re-arm is one more uncorroborated silence window with the mic gated.
PHANTOM_TURN_REARM_LIMIT: int = 3


@runtime_checkable
class ConversationalStateListener(Protocol):
    """V6 consumer seam — notified on every conversational-state transition.

    Async so the consumer (e.g. V6's state-broadcast over the data channel)
    can do its own I/O without blocking the orchestrator. Implementations
    MUST NOT raise — a listener exception would corrupt the turn cycle.
    """

    async def on_state_changed(self, transition: ConversationalTransition) -> None: ...


@runtime_checkable
class TurnActions(Protocol):
    """The effects the orchestrator performs on the loop (T06 wires to V1).

    The orchestrator owns the *decisions*; this seam owns the *mechanism* —
    keeping the orchestrator testable in isolation and the V1 ``StreamingLoop``
    edit minimal (D-V4-X-t05-orchestrator-default).
    """

    async def invoke_model_for_turn(self, final_transcript: Transcript) -> None:
        """Start the V5→V3 generation for a completed user turn (PROCESSING)."""
        ...

    async def cancel_generation(self) -> None:
        """Cancel an in-flight generation that produced no audio yet (D-V4-5)."""
        ...

    async def interrupt(self) -> None:
        """Barge-in stop — cancel TTS + model + clear the outbound rail (D-V4-2)."""
        ...


@runtime_checkable
class SchedulerHandle(Protocol):
    """A cancellable scheduled callback."""

    def cancel(self) -> None: ...


@runtime_checkable
class Scheduler(Protocol):
    """Schedules a delayed async callback (the orchestrator's only timing dep)."""

    def call_later(
        self, delay_s: float, callback: Callable[[], Awaitable[None]]
    ) -> SchedulerHandle: ...


class _AsyncioHandle:
    """Default :class:`SchedulerHandle` — wraps an asyncio task."""

    def __init__(self, task: asyncio.Task[None]) -> None:
        self._task = task

    def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()


class AsyncioScheduler:
    """Default :class:`Scheduler` — ``sleep`` then run, on the running loop."""

    def call_later(
        self, delay_s: float, callback: Callable[[], Awaitable[None]]
    ) -> SchedulerHandle:
        task = asyncio.create_task(self._run(delay_s, callback))
        return _AsyncioHandle(task)

    async def _run(self, delay_s: float, callback: Callable[[], Awaitable[None]]) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        await callback()


class ConversationalOrchestrator:
    """Drives the four-state conversational machine (spec V4 §4).

    Args:
        actions: The loop-effect seam (invoke / cancel / interrupt).
        listener: Optional V6 state-broadcast consumer.
        controller: The endpointing policy (T02); a default is built if omitted.
        detector: The barge-in policy (T03); a default is built if omitted.
        scheduler: The timing seam (T04 default :class:`AsyncioScheduler`).
        clock: UTC-now provider (injected for deterministic tests).
    """

    def __init__(
        self,
        *,
        actions: TurnActions,
        listener: ConversationalStateListener | None = None,
        controller: TurnTakingController | None = None,
        detector: BargeInDetector | None = None,
        scheduler: Scheduler | None = None,
        clock: Callable[[], datetime] | None = None,
        initial_state: ConversationalState = ConversationalState.LISTENING,
        min_authoritative_onset_confidence: float = 0.6,
    ) -> None:
        self._actions = actions
        self._listener = listener
        self._controller = controller or TurnTakingController()
        self._detector = detector or BargeInDetector()
        self._scheduler = scheduler or AsyncioScheduler()
        self._clock = clock or (lambda: datetime.now(UTC))
        # Floor below which a speech onset may NOT, on its own, cancel a reply
        # (PROCESSING continuation) or start a barge-in (PERSONA_SPEAKING) — the
        # V6 operator-pass false-barge-in finding. Bare provider VAD events carry
        # no confidence (``None``) and room noise carries low confidence; both
        # corroborate but must never independently cut the persona off. A real
        # Silero onset fires at/above its activation threshold, so it clears this.
        self._min_authoritative_onset_confidence = min_authoritative_onset_confidence

        # ``PREPARING`` for a greet-first call (Spec 32 A3) — the persona speaks
        # turn 0 before any user input; ``LISTENING`` otherwise (back-compat).
        self._state = initial_state
        self._greet_handle: SchedulerHandle | None = None
        # Per-turn accumulation.
        self._latest_text: str | None = None
        self._final_transcript: Transcript | None = None
        self._last_offset: SpeechEndedEvent | None = None
        # Timer handles.
        self._turn_end_handle: SchedulerHandle | None = None
        self._barge_in_handle: SchedulerHandle | None = None
        # Barge-in candidate tracking.
        self._barge_in_onset: SpeechStartedEvent | None = None
        self._barge_in_resolved = False
        # R9-119: re-arms consumed by the current phantom turn (see _on_turn_end_timer).
        self._phantom_rearms = 0
        # The silence-wait of the most recent turn-end (T08 dual-line latency,
        # D-V4-X-eou-stamp-point) — the V4-attributable threshold cost, surfaced
        # separately from the processing round-trip.
        self._last_endpoint_silence_wait_ms: float | None = None
        # V10-D-2: async-artifact narrations awaiting an idle floor. An artifact's
        # RENDER already went out (the data-channel frame fired when it finished);
        # this queue holds only the SPOKEN mention, which must never play over the
        # user. Drained — coalesced into one utterance — on the next entry into
        # LISTENING (see ``_transition`` / ``_drain_pending_narrations``).
        self._pending_narrations: list[Transcript] = []

    # ----- inspection --------------------------------------------------

    @property
    def state(self) -> ConversationalState:
        return self._state

    @property
    def agent_state(self) -> AgentState:
        return agent_state_for(self._state)

    @property
    def last_endpoint_silence_wait_ms(self) -> float | None:
        """Silence-wait of the most recent turn-end (T08 dual-line latency).

        Feeds :func:`persona_voice.turn_taking.latency.attribute_hops` as the
        ``endpoint_silence_wait_ms`` line — the tuned-threshold cost surfaced
        separately from the processing round-trip (D-V4-X-eou-stamp-point).
        """
        return self._last_endpoint_silence_wait_ms

    def is_agent_speaking(self) -> bool:
        """The mute-window provider V2's VAD adapter consumes (D-V2-X).

        While the persona speaks, the user's inbound VAD must be gated so the
        persona's own voice does not fire a false barge-in. This is the single
        source of truth for ``AgentState.SPEAKING``.

        Also the Spec V8 #1 cost-gate read (D-V8-2): the billed STT leg is
        closed exactly while this is ``True`` (``PERSONA_SPEAKING``).
        """
        return agent_state_for(self._state) is AgentState.SPEAKING

    def is_user_turn_active(self) -> bool:
        """Whether the user's turn is in progress — ``USER_SPEAKING`` or ``PROCESSING``.

        The Spec V8 idle-gate read (D-V8-X-measure-stop-verdict / D-V8-7): the
        billed STT leg streams ONLY during the user's turn (the user speaking,
        or their just-ended turn being processed), and is closed during
        ``PERSONA_SPEAKING`` + ``LISTENING`` idle + ``PREPARING``. A pure state
        projection (sibling of :meth:`is_agent_speaking`); the cost gate
        composes it, the orchestrator owns no gating policy.
        """
        return self._state in (
            ConversationalState.USER_SPEAKING,
            ConversationalState.PROCESSING,
        )

    # ----- V2 SpeechActivityListener -----------------------------------

    async def on_speech_started(self, event: SpeechStartedEvent) -> None:
        """Speech-onset — dispatched by state (D-V4-2/D-V4-5)."""
        if self._state is ConversationalState.LISTENING:
            self._reset_turn()
            await self._transition(TransitionTrigger.USER_SPEECH_STARTED)
        elif self._state is ConversationalState.USER_SPEAKING:
            # The user resumed after a brief pause — that offset was a
            # mid-thought pause, not a turn end. Cancel the pending turn-end.
            self._last_offset = None
            self._cancel_turn_end()
        elif self._state is ConversationalState.PROCESSING:
            # D-V4-5 — the user added more before the persona spoke: a
            # continuation. Cancel the (audio-less) generation and re-open the
            # turn, keeping the accumulated transcript.
            #
            # ...but ONLY for an authoritative onset (a confident Silero onset).
            # A bare provider VAD event or low-confidence room noise must not
            # cancel the reply mid-generation — that produced the empty caption +
            # stuck-Listening in the V6 operator pass. Non-authoritative onsets
            # are dropped here (they still corroborate a real onset elsewhere).
            if not self._is_authoritative_onset(event):
                return
            await self._actions.cancel_generation()
            # cancel_generation() awaited — a concurrent speech-activity source
            # (provider + VAD drains run as separate tasks) can have raced the
            # floor past PROCESSING in the meantime (e.g. its own continuation
            # already fired PROCESSING→USER_SPEAKING). Only fire the transition
            # if we still hold PROCESSING; otherwise the continuation is already
            # reflected and firing USER_CONTINUATION here is an illegal
            # user_speaking→user_continuation move.
            if self._state is not ConversationalState.PROCESSING:
                return
            self._last_offset = None
            await self._transition(TransitionTrigger.USER_CONTINUATION)
        elif self._state is ConversationalState.PERSONA_SPEAKING:
            # A potential barge-in — confirm it over the window (D-V4-2). Only a
            # confident Silero onset may *start* a barge-in candidate; bare
            # provider VAD events / low-confidence noise corroborate but never
            # independently interrupt the persona (V6 operator-pass finding).
            if not self._is_authoritative_onset(event):
                return
            self._barge_in_onset = event
            self._barge_in_resolved = False
            self._cancel_barge_in()
            self._barge_in_handle = self._scheduler.call_later(
                self._detector_confirm_window_s(), self._on_barge_in_confirm
            )
        elif self._state is ConversationalState.PREPARING:
            # Greet-first (Spec 32 A4): the mic should be gated through the
            # greeting. If an onset slips through (a client-side gate timing
            # edge), drop it — log, do NOT fire a transition. USER_SPEECH_STARTED
            # is illegal from PREPARING (A2), so firing it would raise; the guard
            # makes the hand-off safe-by-construction AND recover-don't-crash.
            _logger.warning(
                "user speech onset during PREPARING (greeting); mic-gate likely "
                "raced — dropping the onset, keeping the floor with the greeting"
            )

    def _is_authoritative_onset(self, event: SpeechStartedEvent) -> bool:
        """Whether a speech onset may, on its own, cancel a reply / start a barge-in.

        True only for a confidence-bearing onset at/above the floor (a real
        Silero detection). Bare provider VAD events carry ``confidence is None``
        and room noise carries low confidence — both return False, so they
        corroborate a real onset but never independently cut the persona off
        (the V6 operator-pass false-barge-in finding). The LISTENING turn-start
        and USER_SPEAKING resume paths are intentionally NOT gated — a phantom
        turn produces no transcript and resets harmlessly, whereas a wrongly
        cancelled reply is the damaging case.
        """
        confidence = event.confidence
        return confidence is not None and confidence >= self._min_authoritative_onset_confidence

    async def on_speech_ended(self, event: SpeechEndedEvent) -> None:
        """Speech-offset — arm turn-end, or resolve a pending barge-in."""
        if self._state is ConversationalState.USER_SPEAKING:
            self._last_offset = event
            self._cancel_turn_end()
            self._turn_end_handle = self._scheduler.call_later(
                self._turn_end_delay_s(event), self._on_turn_end_timer
            )
        elif self._state is ConversationalState.PERSONA_SPEAKING:
            await self._resolve_barge_in_on_offset(event)

    # ----- V2 transcripts ----------------------------------------------

    async def on_transcript(self, transcript: Transcript) -> None:
        """Accumulate the turn's transcript (latest text + last final)."""
        self._latest_text = transcript.text
        if transcript.is_final:
            self._final_transcript = transcript

    # ----- loop callbacks (T06 wires these) ----------------------------

    async def notify_model_first_audio(self) -> None:
        """First persona audio reached the rail → PERSONA_SPEAKING.

        Legal from PROCESSING (a normal turn) and from PREPARING (the greet-first
        turn 0, Spec 32 A3). Reaching audio cancels the greet watchdog — the ring
        is over, the greeting is playing.
        """
        if self._state in (ConversationalState.PROCESSING, ConversationalState.PREPARING):
            self._cancel_greet_watchdog()
            await self._transition(TransitionTrigger.MODEL_FIRST_AUDIO)

    async def notify_persona_finished(self) -> None:
        """Persona finished its reply normally → back to LISTENING."""
        if self._state is ConversationalState.PERSONA_SPEAKING:
            await self._transition(TransitionTrigger.PERSONA_FINISHED)

    async def notify_processing_yielded_no_audio(self) -> None:
        """Generation ended without audio (empty/error) → RESET to LISTENING.

        Covers both a normal turn (PROCESSING) and the greet-first turn 0
        (PREPARING) producing nothing — the floor returns to the user either way.
        """
        if self._state in (ConversationalState.PROCESSING, ConversationalState.PREPARING):
            self._cancel_greet_watchdog()
            await self._transition(TransitionTrigger.RESET)

    # ----- async-artifact narration (V10-D-2) --------------------------

    async def notify_artifact_ready(self, narration: Transcript) -> None:
        """A slow async artifact finished — narrate it at the next idle floor.

        The render is already on screen (its data-channel frame fired when the
        artifact completed); this is only the spoken mention. **Strictly
        floor-gated:** the narration speaks NOW only if the floor is genuinely
        idle (``LISTENING``); otherwise it is queued and drained the moment the
        floor next becomes idle, so the persona never talks over the user
        (V10-D-2 / R-1). Multiple narrations pending at drain time are coalesced
        into one utterance.

        Args:
            narration: The synthetic narration prompt the model turns into the
                spoken "it's on screen" line (authored by the caller / the async
                lane; the orchestrator stays persona-agnostic).
        """
        self._pending_narrations.append(narration)
        if self._state is ConversationalState.LISTENING:
            await self._drain_pending_narrations()

    async def _drain_pending_narrations(self) -> None:
        """Fire ONE coalesced narration turn from the idle floor (V10-D-2).

        Re-checks the floor under the single-threaded event loop: only fires when
        ``LISTENING`` and something is pending. Empties the queue first (so a
        re-entrant transition cannot double-fire), coalesces, then enters the
        narration turn via the agent-initiated edge — which reaches
        PERSONA_SPEAKING via the usual first-audio path, or degrades to LISTENING
        via the no-audio RESET, exactly like any turn.
        """
        if not self._pending_narrations or self._state is not ConversationalState.LISTENING:
            return
        pending = self._pending_narrations
        self._pending_narrations = []
        narration = self._coalesce_narrations(pending)
        await self._transition(TransitionTrigger.AGENT_INITIATED)
        await self._actions.invoke_model_for_turn(narration)

    @staticmethod
    def _coalesce_narrations(narrations: list[Transcript]) -> Transcript:
        """Fold pending narrations into one prompt → one spoken utterance.

        ``synthetic=True`` (R9-001): a narration prompt is an internal
        instruction, not the caller's speech — the same class as the turn-0
        greeting nudge — so the persistence boundary persists only the spoken
        narration (the assistant row), never the instruction as a user row.
        """
        text = " ".join(n.text for n in narrations if n.text)
        return Transcript(is_final=True, text=text, confidence=1.0, synthetic=True)

    # ----- greet-on-connect (turn 0; Spec 32 A3) -----------------------

    async def begin_greeting(
        self,
        greeting_transcript: Transcript,
        *,
        warmup: Awaitable[None] | None = None,
        warmup_timeout_s: float = DEFAULT_WARMUP_TIMEOUT_S,
        greet_timeout_s: float = DEFAULT_GREET_TIMEOUT_S,
    ) -> None:
        """Generate turn 0 — the persona greets first, with no user input.

        Runs only from PREPARING (the opening state). Gates on the embedder
        warm-up so the first recall is warm (D-32-X-warmup-gates-turn0); a slow
        warm-up does not block — turn 0 proceeds and the greet watchdog still
        bounds it. If the invocation fails, or no audio reaches the rail within
        ``greet_timeout_s``, the floor degrades to LISTENING so the mic un-gates
        and the user can talk — the call never rings forever (D-32-3).

        Args:
            greeting_transcript: the synthetic turn-0 prompt (the greeting nudge).
            warmup: the embedder warm-up task to await before generating (A1).
            warmup_timeout_s: upper bound on the warm-up wait before proceeding.
            greet_timeout_s: upper bound on reaching first audio before degrading.
        """
        if self._state is not ConversationalState.PREPARING:
            return
        # Announce PREPARING to the client (Spec 32 A4): ring + keep the mic gated
        # until the greeting finishes. A direct broadcast — PREPARING is the
        # initial state (no FSM transition into it), so this frame is the client's
        # "the agent is here, preparing your greeting" signal.
        await self._announce_preparing()
        if warmup is not None:
            try:
                await asyncio.wait_for(asyncio.shield(warmup), warmup_timeout_s)
            except TimeoutError:
                _logger.warning(
                    "embedder warm-up exceeded {timeout}s before turn 0; proceeding "
                    "(the greeting covers the remaining cold load)",
                    timeout=warmup_timeout_s,
                )
        try:
            await self._actions.invoke_model_for_turn(greeting_transcript)
        except Exception:  # noqa: BLE001 — any turn-0 failure must degrade, not crash
            _logger.warning("turn-0 greeting invocation failed; degrading to listening")
            await self.force_reset()
            return
        # Never ring forever: if no first audio arrives in time, degrade.
        self._greet_handle = self._scheduler.call_later(greet_timeout_s, self._on_greet_timeout)

    async def _announce_preparing(self) -> None:
        """Broadcast the initial PREPARING frame (A4 client ring/mic-gate signal)."""
        if self._listener is not None:
            await self._listener.on_state_changed(
                ConversationalTransition(
                    from_state=ConversationalState.PREPARING,
                    to_state=ConversationalState.PREPARING,
                    trigger=TransitionTrigger.GREETING_STARTED,
                    at=self._clock(),
                )
            )

    async def _on_greet_timeout(self) -> None:
        """Greet watchdog — turn 0 produced no audio in time → return the floor."""
        if self._state is ConversationalState.PREPARING:
            _logger.warning("turn-0 greeting produced no audio in time; degrading to listening")
            await self.force_reset()

    def _cancel_greet_watchdog(self) -> None:
        if self._greet_handle is not None:
            self._greet_handle.cancel()
            self._greet_handle = None

    async def force_reset(self) -> None:
        """Force the floor back to the user (RESET → LISTENING) — the recovery path.

        The graceful-degradation / watchdog escape (D-V4-6 / D-V4-X-watchdog-
        timeout): when a hop is stuck (e.g. the barge-in cancel chain hangs past
        the watchdog) the orchestrator forces the machine back to a clean
        LISTENING state rather than leaving the call wedged. A no-op if already
        LISTENING (RESET from LISTENING is not a legal transition).
        """
        if self._state is ConversationalState.LISTENING:
            return
        self._cancel_turn_end()
        self._cancel_barge_in()
        self._cancel_greet_watchdog()
        self._reset_turn()
        await self._transition(TransitionTrigger.RESET)

    # ----- turn-end timer ----------------------------------------------

    async def _on_turn_end_timer(self) -> None:
        if self._state is not ConversationalState.USER_SPEAKING:
            return
        decision = self._controller.decide_turn_end(
            last_offset=self._last_offset,
            settled_text=self._latest_text,
            now=self._clock(),
        )
        if decision.verdict is TurnVerdict.END_TURN:
            # Record the silence-wait as the V4-attributable latency line (T08).
            self._last_endpoint_silence_wait_ms = decision.silence_elapsed_ms
            await self._transition(TransitionTrigger.TURN_ENDED)
            await self._actions.invoke_model_for_turn(self._turn_transcript())
            return
        # R9-119: a WAIT verdict used to be TERMINAL here. The only place that arms this
        # timer is ``on_speech_ended``, so a WAIT with nothing to re-arm it meant the
        # session sat in USER_SPEAKING, mic gated, until the user made a second noise.
        # The controller's own reason for the WAIT ("most likely a noise / false-VAD
        # blip") is exactly the case that never produces a second offset: the noise
        # already stopped. That was the owner's "stuck on listening", on every call,
        # through three specs of threshold tuning that could not touch it because no
        # threshold changes what happens AFTER a WAIT.
        #
        # Only the PHANTOM shape is handled here: no settled text AND an uncorroborated
        # offset. A WAIT with real text (the mid-thought hold token) is a person still
        # talking, and their next offset re-arms the timer as before, so it is left alone.
        if self._is_phantom_turn():
            if self._phantom_rearms < PHANTOM_TURN_REARM_LIMIT and self._last_offset is not None:
                self._phantom_rearms += 1
                self._turn_end_handle = self._scheduler.call_later(
                    self._turn_end_delay_s(self._last_offset), self._on_turn_end_timer
                )
                return
            # Bound reached and still nothing to respond to: it was noise. Give the
            # floor back WITHOUT invoking the model (there is no turn to answer).
            _logger.info(
                "phantom turn abandoned after {n} silent re-arms; back to listening",
                n=self._phantom_rearms,
            )
            self._reset_turn()
            await self.force_reset()
        # Any other WAIT: leave the floor with the user; graceful-degradation bounding
        # of a stuck hold-token is T09's concern.

    # ----- barge-in confirmation ---------------------------------------

    async def _on_barge_in_confirm(self) -> None:
        if (
            self._state is not ConversationalState.PERSONA_SPEAKING
            or self._barge_in_resolved
            or self._barge_in_onset is None
        ):
            return
        sustained_ms = self._elapsed_ms(self._barge_in_onset.ts_emit, self._clock())
        decision = self._detector.decide_barge_in(
            onset=self._barge_in_onset,
            agent_state=self.agent_state,
            sustained_ms=sustained_ms,
            ended=False,
        )
        if decision.verdict is BargeInVerdict.INTERRUPT:
            await self._do_barge_in()

    async def _resolve_barge_in_on_offset(self, offset: SpeechEndedEvent) -> None:
        if self._barge_in_onset is None or self._barge_in_resolved:
            return
        self._cancel_barge_in()
        sustained_ms = self._elapsed_ms(self._barge_in_onset.ts_emit, offset.ts_emit)
        decision = self._detector.decide_barge_in(
            onset=self._barge_in_onset,
            agent_state=self.agent_state,
            sustained_ms=sustained_ms,
            ended=True,
        )
        if decision.verdict is BargeInVerdict.INTERRUPT:
            await self._do_barge_in()
        else:
            # Backchannel / blip — keep the floor with the persona.
            self._barge_in_resolved = True

    async def _do_barge_in(self) -> None:
        """Yield the floor at once: interrupt the loop, then transition."""
        self._barge_in_resolved = True
        self._cancel_barge_in()
        # Effect first (stop the audio + cancel the stale reply), then move the
        # machine — so the mute window opens only after the rail is quiet.
        await self._actions.interrupt()
        self._reset_turn()
        await self._transition(TransitionTrigger.BARGE_IN)

    # ----- transition + helpers ----------------------------------------

    async def _transition(self, trigger: TransitionTrigger) -> None:
        new_state = advance(self._state, trigger)
        if new_state == self._state:
            return
        old_state = self._state
        self._state = new_state
        if self._listener is not None:
            await self._listener.on_state_changed(
                ConversationalTransition(
                    from_state=old_state,
                    to_state=new_state,
                    trigger=trigger,
                    at=self._clock(),
                )
            )
        # V10-D-2: the floor just became idle — drain any queued artifact
        # narration (coalesced). Fired AFTER the LISTENING frame is broadcast so
        # the order reads ...→LISTENING→PROCESSING. The re-entrant transition this
        # triggers lands in PROCESSING (not LISTENING), so it cannot re-drain.
        if new_state is ConversationalState.LISTENING and self._pending_narrations:
            await self._drain_pending_narrations()

    def _turn_transcript(self) -> Transcript:
        """The transcript handed to the model — the last final, or a synthesised
        one from the latest text if no final settled (robust fallback)."""
        if self._final_transcript is not None:
            return self._final_transcript
        return Transcript(is_final=True, text=self._latest_text or "", confidence=1.0)

    def _is_phantom_turn(self) -> bool:
        """A turn with no transcript and no corroborated endpoint: a noise blip, not speech."""
        if (self._latest_text or "").strip():
            return False
        offset = self._last_offset
        return offset is None or not (offset.corroborates or offset.transcript_settled)

    def _reset_turn(self) -> None:
        self._latest_text = None
        self._final_transcript = None
        self._last_offset = None
        self._phantom_rearms = 0
        self._cancel_turn_end()

    def _cancel_turn_end(self) -> None:
        if self._turn_end_handle is not None:
            self._turn_end_handle.cancel()
            self._turn_end_handle = None

    def _cancel_barge_in(self) -> None:
        if self._barge_in_handle is not None:
            self._barge_in_handle.cancel()
            self._barge_in_handle = None

    def _turn_end_delay_s(self, offset: SpeechEndedEvent) -> float:
        corroborated = offset.corroborates or offset.transcript_settled
        ms = (
            self._controller.corroborated_silence_threshold_ms
            if corroborated
            else self._controller.silence_threshold_ms
        )
        return ms / 1000.0

    def _detector_confirm_window_s(self) -> float:
        return self._detector.confirm_window_ms / 1000.0

    @staticmethod
    def _elapsed_ms(start: datetime, end: datetime) -> float:
        return (end - start).total_seconds() * 1000.0
