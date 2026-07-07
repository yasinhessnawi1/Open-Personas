"""Wiring the orchestrator to the V1 loop + session (spec V4 T06).

The composition layer that connects the pure V4 orchestration (T01–T04) to
V1's :class:`persona_voice.loop.streaming.StreamingLoop` and
:class:`persona_voice.session.state_machine.SessionStateMachine`:

* :class:`LoopTurnActions` — the :class:`TurnActions` seam backed by the loop.
  It runs each model invocation as a **cancellable task** so V4 can stop a
  stale generation promptly on barge-in / continuation (the model-side of the
  three-things-stopping-together, spec V4 §8 / D-V4-4). On interrupt it cancels
  the task (whose ``finally`` emits the single ``AGENT_STOPPED_SPEAKING``) and
  flushes the rail + cancels TTS via the loop's notify-free teardown half.

* :class:`SessionEventBridge` — a :class:`ConversationalStateListener` that
  feeds the user-side conversational transitions onto V1's existing
  ``SessionStateMachine.notify`` / ``SessionEventListener`` seam **without
  modifying any transition logic** (the agent-side events are already emitted
  by the loop's ``invoke_model_for_turn``; this bridge only adds the
  ``USER_STARTED_SPEAKING`` / ``USER_STOPPED_SPEAKING`` events that nothing
  emitted before).

* :class:`CompositeStateListener` — fans one transition out to several
  listeners (the session bridge + V6's renderer).

* :func:`wire_orchestrated_loop` — the composition root: builds the
  orchestrator backed by the loop, registers it as the loop's
  ``speech_activity`` listener + ``orchestrator``, and returns it ready to run.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from persona_voice.session.state_machine import SessionLifecycleEvent
from persona_voice.turn_taking.heard_words import BargedReply
from persona_voice.turn_taking.orchestrator import ConversationalOrchestrator
from persona_voice.turn_taking.states import ConversationalState

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime

    from persona_voice.loop.streaming import HeardReply, StreamingLoop, Transcript
    from persona_voice.session.state_machine import SessionStateMachine
    from persona_voice.turn_taking.barge_in import BargeInDetector
    from persona_voice.turn_taking.controller import TurnTakingController
    from persona_voice.turn_taking.heard_words import TurnTranscriptListener
    from persona_voice.turn_taking.orchestrator import (
        ConversationalStateListener,
        Scheduler,
    )
    from persona_voice.turn_taking.states import ConversationalTransition

__all__ = [
    "CompositeStateListener",
    "CompositeTurnTranscriptListener",
    "HeardWordsBridge",
    "LoopTurnActions",
    "SessionEventBridge",
    "wire_orchestrated_loop",
]


# Watchdog hard-timeout for the barge-in cancel chain (D-V4-X-watchdog-timeout):
# if the cancelled model task does not unwind within this budget, stop waiting so
# the floor still returns to the user (the lower bound of V3 R-V3-5's LiveKit
# INTERRUPTION_TIMEOUT 2-4 s range).
DEFAULT_CANCEL_WATCHDOG_S: float = 2.0


class LoopTurnActions:
    """:class:`TurnActions` backed by a :class:`StreamingLoop` (cancellable).

    Runs each model invocation as its own task so barge-in / continuation can
    cancel it promptly. Single-turn at a time — a new invocation cancels any
    prior in-flight one defensively.
    """

    def __init__(
        self, loop: StreamingLoop, *, cancel_watchdog_s: float = DEFAULT_CANCEL_WATCHDOG_S
    ) -> None:
        self._loop = loop
        self._cancel_watchdog_s = cancel_watchdog_s
        self._task: asyncio.Task[None] | None = None

    async def invoke_model_for_turn(self, final_transcript: Transcript) -> None:
        """Start the V5→V3 generation for a completed turn as a cancellable task."""
        await self._cancel_inflight()
        self._task = asyncio.create_task(
            self._loop.invoke_model_for_turn(final_transcript),
            name="v4-model-turn",
        )

    async def cancel_generation(self) -> None:
        """Cancel an in-flight, audio-less generation (D-V4-5 continuation)."""
        await self._cancel_inflight()

    async def interrupt(self) -> None:
        """Barge-in stop — cancel the model task + flush the rail + cancel TTS.

        The cancelled task's ``finally`` emits the single
        ``AGENT_STOPPED_SPEAKING``; the loop's notify-free teardown clears the
        outbound queue and cancels TTS (no duplicate lifecycle event).
        """
        await self._cancel_inflight()
        await self._loop.flush_outbound_and_cancel_tts()

    async def _cancel_inflight(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        # Watchdog (D-V4-X-watchdog-timeout): bound the wait so a hung teardown
        # never blocks the floor returning to the user. On timeout the task is
        # left detached (already cancelled); the orchestrator still moves on.
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), self._cancel_watchdog_s)


class SessionEventBridge:
    """Feeds user-side transitions onto V1's ``SessionEventListener`` seam.

    A :class:`ConversationalStateListener`. Maps the conversational machine's
    user-floor transitions to :class:`SessionLifecycleEvent` and dispatches via
    :meth:`SessionStateMachine.notify` — **no transition-logic change** to the
    session machine (it just receives events). Agent-side events
    (``AGENT_STARTED_SPEAKING`` / ``AGENT_STOPPED_SPEAKING``) are already
    emitted by the loop's ``invoke_model_for_turn``, so this bridge does not
    re-emit them.
    """

    def __init__(self, session: SessionStateMachine) -> None:
        self._session = session

    async def on_state_changed(self, transition: ConversationalTransition) -> None:
        event: SessionLifecycleEvent | None = None
        if transition.to_state is ConversationalState.USER_SPEAKING:
            event = SessionLifecycleEvent.USER_STARTED_SPEAKING
        elif transition.from_state is ConversationalState.USER_SPEAKING:
            # Leaving the user's turn (→ PROCESSING on turn-end, or → LISTENING
            # on a false-start reset).
            event = SessionLifecycleEvent.USER_STOPPED_SPEAKING
        if event is not None:
            await self._session.notify(event)


class CompositeStateListener:
    """Fans one transition out to several :class:`ConversationalStateListener`s."""

    def __init__(self, listeners: Sequence[ConversationalStateListener]) -> None:
        self._listeners = tuple(listeners)

    async def on_state_changed(self, transition: ConversationalTransition) -> None:
        for listener in self._listeners:
            await listener.on_state_changed(transition)


class CompositeTurnTranscriptListener:
    """Fans one committed (heard) reply out to several :class:`TurnTranscriptListener`s.

    Lets more than one seam observe each turn's commit off the single loop
    ``turn_transcript_listener`` slot — the V5 memory recorder AND (Spec A9) the
    origination gate's barge-aware commit hook (``note_spoken_turn_committed``,
    adapted to this Protocol). Listeners are notified in order; each MUST NOT raise
    (the loop runs the commit in its ``finally``).
    """

    def __init__(self, listeners: Sequence[TurnTranscriptListener]) -> None:
        self._listeners = tuple(listeners)

    async def on_reply_committed(self, reply: BargedReply) -> None:
        for listener in self._listeners:
            await listener.on_reply_committed(reply)


class HeardWordsBridge:
    """Adapts V1's :class:`HeardReply` onto V5's :class:`TurnTranscriptListener`.

    Implements the loop's ``ReplyHeardListener`` seam; maps each per-turn
    ``HeardReply`` to a :class:`BargedReply` and forwards it to V5's memory-write
    seam (D-V4-4) — the truncated-as-heard text crosses the boundary intact.
    """

    def __init__(self, listener: TurnTranscriptListener) -> None:
        self._listener = listener

    async def on_reply_heard(self, reply: HeardReply) -> None:
        await self._listener.on_reply_committed(
            BargedReply(
                heard_text=reply.text,
                truncated=reply.truncated,
                token_count=reply.token_count,
            )
        )


def wire_orchestrated_loop(
    *,
    loop: StreamingLoop,
    session: SessionStateMachine,
    controller: TurnTakingController | None = None,
    detector: BargeInDetector | None = None,
    scheduler: Scheduler | None = None,
    clock: Callable[[], datetime] | None = None,
    state_listener: ConversationalStateListener | None = None,
    turn_transcript_listener: TurnTranscriptListener | None = None,
    cancel_watchdog_s: float = DEFAULT_CANCEL_WATCHDOG_S,
    initial_state: ConversationalState = ConversationalState.LISTENING,
) -> ConversationalOrchestrator:
    """Build + wire the orchestrator to ``loop`` and ``session`` (composition root).

    Returns the orchestrator, with the loop registered to drain transcripts into
    it (``loop.orchestrator``) and to deliver speech-activity events
    (``loop.speech_activity``). The session's existing event seam receives the
    user-side conversational transitions; ``state_listener`` (e.g. V6's
    renderer), if given, also receives every transition. If
    ``turn_transcript_listener`` (V5's memory-write seam) is given, the loop's
    per-turn :class:`HeardReply` is adapted to :class:`BargedReply` and forwarded
    to it (D-V4-4). ``cancel_watchdog_s`` bounds the barge-in cancel chain.
    """
    actions = LoopTurnActions(loop, cancel_watchdog_s=cancel_watchdog_s)
    listeners: list[ConversationalStateListener] = [SessionEventBridge(session)]
    if state_listener is not None:
        listeners.append(state_listener)
    composite = CompositeStateListener(listeners)
    orchestrator = ConversationalOrchestrator(
        actions=actions,
        listener=composite,
        controller=controller,
        detector=detector,
        scheduler=scheduler,
        clock=clock,
        initial_state=initial_state,
    )
    loop.orchestrator = orchestrator
    loop.speech_activity = orchestrator
    if turn_transcript_listener is not None:
        loop.turn_transcript_listener = HeardWordsBridge(turn_transcript_listener)
    return orchestrator
