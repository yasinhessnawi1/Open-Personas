"""The persona-conditioned voice ModelReplyProducer (spec V5 T4 + T7).

This fills V4's ``ModelReplyProducer`` seam (``async __call__(final_transcript)
-> AsyncIterator[str]``) with the real generation: the model V4 invokes per turn
is Persona's tier-routed, persona-conditioned, streaming model — the same
conditioning as the text persona (criteria 1+2; the §8 persona-bypass line), not
a thin voice prompt.

One turn:

1. build the full persona-conditioned prompt via :class:`VoicePromptAssembler`
   (shared ``PromptBuilder`` + retrieval — D-V5-6, no bypass);
2. choose the tier (rule-based, instant — Spec 05) and, within it, the best model
   meeting the voice first-token-latency gate via :class:`VoiceRoutingPolicy`
   (D-V5-2); reorder the tier backend so the chosen model is primary (the Spec 23
   ``reorder_primary`` seam, mirroring the text loop);
3. stream the reply token-by-token from ``ChatBackend.chat_stream`` straight into
   V3 — only ``chunk.delta`` (spoken text) is yielded; ``chunk.reasoning`` is
   never forwarded (the practical "force non-reasoning" for voice, D-V5-2);
4. tools (T7, D-V5-4/5, the conservative-conversational v1): the voice-viable +
   deferred subset is offered to the model. A **voice-viable** tool call is
   narrated with a first-class preamble ("let me look that up"), run under a hard
   latency bound, and its result fed back for one re-prompt (one tool round — the
   conservative v1, not the text loop's multi-round sub-loop). A **deferred**
   (heavy) tool call is acknowledged in voice and emitted as a
   :class:`DeferredArtifact` intent to be produced off the live path (F5);
5. first-token latency is stamped per round into the shared
   :class:`~persona_runtime.routing.FirstTokenLatencyTracker` (the D-V5-2 routing
   refinement + the same number Spec 18 records) and the optional
   ``first_token_listener`` is notified with the wall-clock instant (the VoiceLog
   ``llm_first_token_at`` stamping seam).

Cancellation (R-V5-4, verified in T5): the generator is a plain ``async for`` over
``chat_stream``; when V4 cancels the consuming task on barge-in the
``CancelledError`` propagates into ``chat_stream`` whose ``async with`` provider
stream closes cleanly — no fire-and-forget escapes this scope.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.schema.conversation import ConversationMessage
from persona.schema.tools import ToolCall
from persona.tools import format_tool_result
from persona_runtime.activity import dispatch_with_activity
from persona_runtime.agentic.events import RunEvent
from persona_runtime.emotional import ConvertMode, FeelingTagConverter
from persona_runtime.graph_voice import start_graph_retrieval, take_graph_if_ready
from persona_runtime.graph_window import set_recent_window_from_messages
from persona_runtime.routing import RoutingContext, classifiers
from persona_runtime.routing.model_selection import reorder_primary
from persona_runtime.safety_intercept import InterceptAction, classify_user_message
from persona_runtime.schedule_claim import (
    SCHEDULE_CLAIM_LEXICON_VERSION,
    detect_schedule_claim,
    render_schedule_correction,
)

from persona_voice.model.expressivity import VoiceExpressivity, resolve_expressivity
from persona_voice.model.history import VoiceHistoryCompactor
from persona_voice.model.origination_gate import DelegatedTurnIntent, VoiceOriginationDecision
from persona_voice.model.prompt_assembler import VoicePromptAssembler
from persona_voice.model.routing import VoiceRoutingPolicy
from persona_voice.model.tools import (
    DEFAULT_VOICE_TOOL_TIMEOUT_S,
    DeferredArtifact,
    VoiceToolDisposition,
    VoiceToolNarrator,
    VoiceToolPolicy,
    run_tool_with_latency_bound,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona.backends import ChatBackend
    from persona.backends.types import ToolSpec

    from persona_voice.billing import VoiceTurnBillingMeter
    from persona_voice.loop.streaming import Transcript
    from persona_voice.model.memory import VoiceTurnRecorder
    from persona_voice.model.turn_context import VoiceTurnContext

__all__ = ["VoiceModelReplyProducer"]

_logger = get_logger("voice.reply_producer")

_DEFAULT_MAX_TOKENS = 4096


@dataclass
class _RoundTiming:
    """Per-generation first-token bookkeeping (reset per round; notified once)."""

    t_start: float
    notified: bool = False


class VoiceModelReplyProducer:
    """Persona-conditioned, tier-routed, streaming, cancellable voice generation.

    Constructed once per voice session over a :class:`VoiceTurnContext`. Satisfies
    V4's ``ModelReplyProducer`` Protocol; V4 invokes it per turn and cancels it on
    barge-in.

    Args:
        context: The session-bound runtime collaborators.
        routing_policy: The voice first-token-latency routing policy (D-V5-2); a
            default :class:`VoiceRoutingPolicy` is built if omitted.
        tool_policy: The voice-tools scope policy (D-V5-4); default built if omitted.
        narrator: The preamble-as-contract narrator (D-V5-5); default if omitted.
        tool_timeout_s: Hard wall-clock bound for a live voice tool (D-V5-4).
        first_token_listener: Optional callback notified with the wall-clock
            instant the LLM's first token arrives — the VoiceLog
            ``llm_first_token_at`` stamping seam. MUST NOT raise.
        deferred_artifact_listener: Optional callback notified when a deferred
            (heavy) tool is acknowledged in voice — the off-path F5 intent
            (D-V5-4-f5-artifact-shape). MUST NOT raise.
        async_artifact_listener: Optional fire-and-forget hook (V10 T3,
            V10-D-X-async-lane) — the off-turn production lane's ``submit``. When
            present, an ``ASYNC_ARTIFACT`` call (e.g. ``generate_image``) is
            acknowledged inline and handed to the lane (render-when-ready +
            floor-gated narration); when absent the call falls back to the
            deferred acknowledgement so it is never stranded. MUST NOT raise.
        on_event: Optional async sink for granular activity events (V10 T1,
            V10-D-X-producer-sink). When present, a live voice tool dispatches
            through P2's shared :func:`dispatch_with_activity` seam, so each call
            emits a paired ``activity_start``/``activity_end`` :class:`RunEvent`
            in the SAME vocabulary chat/runs use — the voice transport (the
            LiveKit data channel) carries it instead of SSE, but the events are
            identical (no parallel voice event format — the one thing P2 forbids).
            ``None`` (the default) keeps the bare ``toolbox.dispatch`` path,
            byte-identical to V5 — no instrumentation cost, no talk-only
            regression (acceptance #6).
        clock: UTC-now provider (injected for deterministic tests).
    """

    def __init__(
        self,
        context: VoiceTurnContext,
        *,
        routing_policy: VoiceRoutingPolicy | None = None,
        tool_policy: VoiceToolPolicy | None = None,
        narrator: VoiceToolNarrator | None = None,
        tool_timeout_s: float = DEFAULT_VOICE_TOOL_TIMEOUT_S,
        first_token_listener: Callable[[datetime], None] | None = None,
        deferred_artifact_listener: Callable[[DeferredArtifact], None] | None = None,
        async_artifact_listener: Callable[[ToolCall], None] | None = None,
        expressivity_listener: Callable[[VoiceExpressivity | None], None] | None = None,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        turn_recorder: VoiceTurnRecorder | None = None,
        delegation_listener: Callable[[DelegatedTurnIntent], None] | None = None,
        turn_meter: VoiceTurnBillingMeter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ctx = context
        self._assembler = VoicePromptAssembler(context)
        self._routing = routing_policy or VoiceRoutingPolicy()
        self._history = VoiceHistoryCompactor(context.history_manager)
        self._tool_policy = tool_policy or VoiceToolPolicy()
        self._narrator = narrator or VoiceToolNarrator()
        self._tool_timeout_s = tool_timeout_s
        self._first_token_listener = first_token_listener
        self._deferred_listener = deferred_artifact_listener
        # V10 T3 (V10-D-X-async-lane): the off-turn production lane's submit hook.
        # When present, an ASYNC_ARTIFACT call (e.g. generate_image) is handed to
        # the lane (render-when-ready + floor-gated narration) instead of being
        # acknowledged-and-deferred; when absent it falls back to the deferred
        # acknowledgement (never strands). Fire-and-forget, like the deferred one.
        self._async_artifact_listener = async_artifact_listener
        # V12 T3 (V12-D-2/D-4): the per-utterance expressivity hook. When present, the
        # persona's first declared feeling-tag (captured off the STRIP converter, still
        # stripped from the spoken audio) is resolved to a VoiceExpressivity and handed
        # to the TTS side, which drives Cartesia's generation_config (V12-D-1). Absent ⇒
        # today's flat read. Fire-and-forget + guarded (V12-D-5): it must never break the
        # spoken stream, so a raising listener degrades to flat, never to a broken turn.
        self._expressivity_listener = expressivity_listener
        # V10 T1 (V10-D-X-producer-sink): the activity-event sink. When present,
        # tool dispatch routes through P2's ``dispatch_with_activity`` seam.
        self._on_event = on_event
        # The unified-memory recorder (T8). The producer notes this turn's user
        # transcript so the recorder can correlate it with the heard reply on
        # commit (D-V5-X-memory-write-on-commit); the actual write happens on V4's
        # on_reply_committed, never here (no speculative mid-stream write).
        self._turn_recorder = turn_recorder
        # A9 (A9-D-5/D-7): the delegation sink. When the origination gate confirms a spoken
        # task/schedule ask, the confirmed VERBATIM ask is handed here to be delegated to the
        # chat pipeline (the durable ``delegated_turn`` job — T4). ``None`` ⇒ the gate still runs
        # (echo/confirm for the ear) but nothing is delegated (T3 placement; T4 wires it live).
        self._delegation_listener = delegation_listener
        # Spec M3 (T6b-1): the per-turn owner-billing meter. When present, the
        # producer feeds it this turn's real metered quantities as they occur —
        # characters sent to TTS (each spoken chunk) + the model round's token
        # usage (the final stream chunk) — for the off-loop deduct the turn
        # recorder fires on commit. ``None`` ⇒ unmetered voice (byte-identical to
        # pre-M3): the feed calls are simply skipped.
        self._turn_meter = turn_meter
        self._clock = clock or (lambda: datetime.now(UTC))
        # Rotates the preamble across turns so the filler is not robotic (D-V5-5).
        self._preamble_index = 0

    def set_turn_meter(self, meter: VoiceTurnBillingMeter) -> None:
        """Late-bind the per-turn billing meter (Spec M3, T6b-1).

        The composition root builds the meter after the STT/TTS backends exist
        (their served provider identities feed the pricing), but the producer is
        constructed earlier — so the runner injects it here, before ``run()`` fires
        the first turn (the same late-binding posture as the broadcaster/lane holders).
        """
        self._turn_meter = meter

    async def __call__(self, final_transcript: Transcript) -> AsyncIterator[str]:
        """Return the token stream for one completed user turn (V4 awaits this)."""
        return self._generate(final_transcript)

    async def _generate(self, final_transcript: Transcript) -> AsyncIterator[str]:
        """Stream the persona-conditioned reply token-by-token (spoken text only)."""
        ctx = self._ctx
        user_message = final_transcript.text

        # R1-hard out-of-band bypass (Spec V11, V11-D-5) — the same gate as the chat
        # and agentic loops. An acute, explicit W1 transcript takes the persona (and
        # the lock that suppresses crisis-noticing) out of the loop entirely: speak
        # the deterministic SHORT spoken safe-completion variant (Group-B note 2),
        # never the chat resource block, and never call the model. Classified before
        # any routing/retrieval so the bypass skips all of it. The user turn is noted
        # for the unified-memory write; V4's commit path records what was spoken. One
        # acute path: bypass (never inject-and-generate). The composed classify (V11
        # lexical ∪ the R6 encoder) is CPU-bound when the encoder is wired, so it runs on
        # a worker thread — the voice event loop must never block on the forward pass (the
        # voice-event-loop-starvation rule; R6-D-2). Fail-soft→R0 lives inside classify
        # (encoder error / not-yet-warm / timeout ⇒ lexical), so this always returns.
        safety_verdict = await asyncio.to_thread(
            classify_user_message,
            user_message,
            locale=ctx.persona.identity.language_default,
            encoder=ctx.crisis_encoder,
        )
        if safety_verdict.action is InterceptAction.HARD and safety_verdict.completion is not None:
            if self._turn_recorder is not None:
                self._turn_recorder.note_user_message(
                    user_message, synthetic=final_transcript.synthetic
                )
            yield safety_verdict.completion.voice_text
            return

        # A9 (A9-D-1/D-5/D-7): the voice task-origination gate — placed HERE, strictly AFTER the
        # R1-hard safety bypass (a crisis turn returned above; origination is never consulted —
        # criterion 3) and BEFORE routing/retrieval/generation. The gate's cheap
        # ``detect_standing_cue`` regex gates the model judge, so a no-cue turn (the overwhelming
        # case) pays only microseconds and falls through unchanged — nothing model-judged runs on
        # the loop for it (criterion 2). When the gate OWNS the turn it speaks the echo/confirm for
        # the ear and, on a clean confirm, DELEGATES the verbatim ask to the chat pipeline (voice
        # never executes with the mid model — the create runs off this loop, frontier tier, one
        # audited path). Graph stays OFF (the gate never reads it). ``None`` gate ⇒ byte-identical.
        gate = ctx.origination_gate
        if gate is not None:
            # Fail-soft (T10): a gate failure (a keyless/over-budget judge that escapes the
            # recognizer's own ask-once degrade, or any bug) must degrade to TODAY's clean call —
            # ordinary generation — never a broken or stalled turn. The gate is additive; it can
            # only ever add a spoken echo, never break the conversation.
            try:
                decision: VoiceOriginationDecision | None = await gate.on_user_turn(user_message)
            except Exception:  # noqa: BLE001 — a gate failure degrades to today's clean call
                _logger.warning("voice origination gate failed; falling through to ordinary turn")
                decision = None
            if decision is not None and decision.owns_turn:
                if self._turn_recorder is not None:
                    self._turn_recorder.note_user_message(
                        user_message, synthetic=final_transcript.synthetic
                    )
                if decision.verbatim_ask is not None and self._delegation_listener is not None:
                    # Delegate the VERBATIM ask (never the mid-model draft) for the frontier to
                    # re-parse + execute. Set on an origination CONFIRM (the spoken echo + "yes" was
                    # the confirmation — A9-D-7) OR a spoken STEERING ask (pause/resume/cancel/
                    # reschedule — T7, the same crossing).
                    self._delegation_listener(
                        DelegatedTurnIntent(
                            conversation_id=ctx.conversation.conversation_id,
                            verbatim_ask=decision.verbatim_ask,
                            persona_id=ctx.persona_id,
                        )
                    )
                assert decision.spoken is not None  # owns_turn ⇒ spoken is not None
                yield decision.spoken
                return
            # ORDINARY → the gate did not own the turn; proceed to normal generation unchanged.

        # K4 (K4-D-2): publish this turn's recent-conversation window BEFORE the graph
        # query is kicked off, so the gate reads the conversation (not the bare query) and
        # never evades a topic the caller just raised mid-call — the worst failure on a
        # live call. ``asyncio.to_thread`` (and ``create_task``) copy this contextvar into
        # the worker thread, the same propagation the owner scope already relies on.
        set_recent_window_from_messages(ctx.conversation.messages)
        # K3 (D-K3-6): kick the owner-scoped graph query off NOW, so it runs
        # CONCURRENTLY with the routing/selection pre-model work below. It is
        # taken just before assembly and only if already ready — never awaited on
        # the critical path, so it adds zero serial wall-clock to TTFT. ``None``
        # graph_retrieval ⇒ the turn runs graph-off (additive).
        graph_task = (
            start_graph_retrieval(ctx.graph_retrieval, user_message)
            if ctx.graph_retrieval is not None
            else None
        )
        # Note this turn's user transcript for the unified-memory write that the
        # recorder performs on commit (T8 correlation key — D-V5-X). ``synthetic``
        # rides along (R9-001): an internally-originated prompt (greeting nudge /
        # narration) must not persist as user speech at the commit boundary.
        if self._turn_recorder is not None:
            self._turn_recorder.note_user_message(
                user_message, synthetic=final_transcript.synthetic
            )

        routing_context = self._routing_context(user_message)
        tier = self._choose_tier(routing_context)
        selection = self._routing.select_model(ctx, tier=tier, routing_context=routing_context)
        backend = ctx.tier_registry.get(tier)
        if ctx.intelligent_router is not None and ctx.persona.routing.intelligent.enabled:
            # Make the gated model primary (no-op when it already is — Spec 23 seam).
            backend = reorder_primary(backend, selection.model)

        max_tokens = self._max_tokens(backend)
        # The fast, never-blocking history view (D-V5-3); the slow compaction runs
        # off the critical path in a background inter-turn task (T8).
        history = self._history.live_history(ctx.conversation)
        # Persona-store retrieval runs the bge-small embedder's synchronous,
        # CPU-bound ``.encode()`` (one recall per variable store, per turn). It MUST
        # run OFF the event loop or it starves the loop: while inference blocks, the
        # loop cannot service LiveKit heartbeats or the Deepgram/Cartesia WebSocket
        # handshakes, which is the live voice incident's root cause (three outbound
        # connections timing out simultaneously). Both the graph-on and graph-off
        # paths offload via ``asyncio.to_thread`` (the codebase's canonical
        # CPU-offload primitive — D-02-17 / graph_voice precedent).
        context = await asyncio.to_thread(
            self._assembler.retrieve, user_message, history_turns=len(history)
        )
        # The R1-soft override directive for this turn (from the single classify
        # above). HARD already returned via the bypass, so only SOFT carries a
        # directive here and NONE is ``None`` — byte-identical, R0 floor intact.
        safety_directive = safety_verdict.soft_directive
        if graph_task is not None:
            # K3-D-6 — GENUINE overlap: the off-thread retrieval above gave the
            # concurrently-running graph query a window to finish in. We take the
            # graph only if it is ready by now; otherwise the turn proceeds
            # graph-off (additive). Zero NEW serial work on the TTFT path.
            graph = take_graph_if_ready(graph_task)
            prompt = self._assembler.build(
                user_message,
                history=history,
                max_tokens=max_tokens,
                graph=graph,
                context=context,
                safety_directive=safety_directive,
            )
        else:
            prompt = self._assembler.build(
                user_message,
                history=history,
                max_tokens=max_tokens,
                context=context,
                safety_directive=safety_directive,
            )

        timing = _RoundTiming(t_start=time.perf_counter())
        tools = self._offered_specs(backend)

        # A9 (T8, A9-D-6): accumulate the complete spoken reply so the confabulation guard can run
        # on it once at end-of-stream. Reaching THIS (ordinary generation) path proves the turn did
        # NOT delegate — the gate-owned echo/confirm/delegate turns all early-returned above — so a
        # new-schedule success CLAIM here is a hallucination (voice never creates a schedule).
        spoken_out: list[str] = []

        # First round: stream the reply (and any tool call the model decides on).
        round_text: list[str] = []
        calls: list[ToolCall] = []
        async for delta in self._stream(
            backend, prompt, tools, max_tokens, timing, round_text, calls
        ):
            spoken_out.append(delta)
            yield delta

        if calls:
            # Conservative v1: at most one tool, one re-prompt round (D-V5-4).
            async for delta in self._handle_tool_call(
                backend, prompt, max_tokens, timing, round_text, calls[0]
            ):
                spoken_out.append(delta)
                yield delta

        # T8: the confabulation guard (A10's detector, reused verbatim) on the COMPLETE reply. On a
        # claimed-but-not-created hit, the honest correction is spoken as the stream tail — so it is
        # HEARD, and the unified-memory recorder folds it into episodic on commit (the graph can
        # never mint a confabulated "I scheduled it" fact from a non-delegated voice turn).
        correction = self._schedule_claim_correction("".join(spoken_out))
        if correction is not None:
            yield correction

    def _schedule_claim_correction(self, reply_text: str) -> str | None:
        """The honest correction if this non-delegated reply falsely claims a schedule (A9-D-6).

        Reuses A10's ``detect_schedule_claim`` / ``render_schedule_correction`` verbatim (one
        detector, one lexicon — anti-fork). ``None`` when the reply makes no new-schedule claim.
        """
        signal = detect_schedule_claim(reply_text)
        if signal is None:
            return None
        _logger.info(
            "voice schedule-claim correction appended (claimed without a delegation) "
            "matched={matched} lexicon={version}",
            matched=signal.matched,
            version=SCHEDULE_CLAIM_LEXICON_VERSION,
        )
        return f"\n\n{render_schedule_correction()}"

    async def _handle_tool_call(
        self,
        backend: ChatBackend,
        base_prompt: list[ConversationMessage],
        max_tokens: int,
        timing: _RoundTiming,
        round_text: list[str],
        call: ToolCall,
    ) -> AsyncIterator[str]:
        """Run the conservative single voice tool round (D-V5-4/5 / V10-D-3)."""
        disposition = self._tool_policy.classify(call.name)
        if disposition is VoiceToolDisposition.ASYNC_ARTIFACT:
            if self._async_artifact_listener is not None:
                # V10-D-3: a slow visual artifact — acknowledge inline ("…on
                # screen…"), hand production to the off-turn lane, and return to
                # the fast path NOW. The render (its data-channel frame) and the
                # spoken "it's on screen" confirmation happen render-when-ready,
                # decoupled from this turn's audio timing — never inline, never
                # dead air, barge-in still works.
                yield self._narrator.async_artifact_line
                self._async_artifact_listener(call)
                return
            # No lane wired (e.g. the runner has not connected it) — fall back to
            # the deferred acknowledgement so the call is never stranded.
            disposition = VoiceToolDisposition.DEFERRED
        if disposition is VoiceToolDisposition.DEFERRED:
            # Heavy capability — acknowledge in voice, produce off-path (F5).
            ack = self._narrator.deferral_line
            yield ack
            if self._deferred_listener is not None:
                self._deferred_listener(
                    DeferredArtifact(
                        tool_name=call.name, arguments=call.args, spoken_acknowledgement=ack
                    )
                )
            return
        if disposition is not VoiceToolDisposition.VOICE_VIABLE or self._ctx.toolbox is None:
            return  # text-only (not offered) / no toolbox — nothing to run

        # Voice-viable: narrate the preamble (contract), run bounded, re-prompt once.
        yield self._narrator.preamble(index=self._preamble_index)
        self._preamble_index += 1
        # V10 T1 (V10-D-X-producer-sink): dispatch through P2's shared seam so the
        # call emits a paired activity_start/activity_end over ``on_event`` (the
        # unified vocabulary). With ``on_event=None`` this is the bare
        # ``toolbox.dispatch`` — byte-identical to V5. Still under the V5 hard
        # latency bound (the seam returns the same ``Awaitable[ToolResult]``).
        outcome = await run_tool_with_latency_bound(
            dispatch_with_activity(self._ctx.toolbox, call, on_event=self._on_event, step=-1),
            timeout_s=self._tool_timeout_s,
        )
        if outcome.timed_out or outcome.result is None:
            yield self._narrator.overflow_line  # never strand the call (D-V5-4)
            return

        # V10 (T4): emit the artifact-bearing tool_result frame so an inline tool
        # that produces an artifact (e.g. render_diagram) renders in the
        # FileRendererPanel — the inline analog of the async lane's render frame
        # (P2-D-3 keep-both: the activity_* badge already fired during dispatch).
        if self._on_event is not None:
            await self._on_event(
                RunEvent.tool_result(
                    -1, call.name, outcome.result, kind=self._ctx.toolbox.kind_for(call.name)
                )
            )

        followup = self._followup_prompt(backend, base_prompt, round_text, call, outcome.result)
        async for delta in self._stream(backend, followup, None, max_tokens, timing, [], []):
            yield delta

    async def _stream(
        self,
        backend: ChatBackend,
        prompt: list[ConversationMessage],
        tools: list[ToolSpec] | None,
        max_tokens: int,
        timing: _RoundTiming,
        text_out: list[str],
        calls_out: list[ToolCall],
    ) -> AsyncIterator[str]:
        """Stream one model round: yield spoken deltas, collect tool calls + timing.

        Spoken text only — ``chunk.reasoning`` is never forwarded to TTS. First
        token latency is measured from this round's start (``timing.t_start`` reset
        here) and recorded once per generation.
        """
        timing.t_start = time.perf_counter()
        names: dict[str, str] = {}
        args_json: dict[str, str] = {}
        order: list[str] = []
        # N5-A8 (N5-D-1/D-7): the voice tag-STRIP safety floor. A raw ``{{#…}}`` must
        # never reach TTS (a spoken tag is the audio leak), including split across chunks.
        # ``text_out`` keeps the RAW delta (the tool-followup prompt's context); only the
        # TTS-bound yield is stripped. V12 (V12-D-2) turns tag EMISSION on in voice and
        # OBSERVES the tag via ``on_feeling`` to drive expressivity — a pure notification
        # that does not touch the strip (the tag is still removed from the audio); the
        # criterion-3 floor is preserved by reuse of the exact converter.
        strip_converter = FeelingTagConverter(ConvertMode.STRIP, on_feeling=self._capture_stance())
        async for chunk in backend.chat_stream(
            prompt, tools=tools, temperature=0.0, max_tokens=max_tokens
        ):
            if chunk.delta:
                if not timing.notified:
                    timing.notified = True
                    if self._ctx.latency_tracker is not None:
                        self._ctx.latency_tracker.record(
                            backend.model_name, (time.perf_counter() - timing.t_start) * 1000.0
                        )
                    if self._first_token_listener is not None:
                        self._first_token_listener(self._clock())
                text_out.append(chunk.delta)
                spoken = strip_converter.feed(chunk.delta)
                if spoken:
                    # Spec M3 (T6b-1): count characters sent to TTS (post-STRIP —
                    # exactly the audio-bound text) for the served TTS provider's meter.
                    if self._turn_meter is not None:
                        self._turn_meter.note_tts_chars(len(spoken))
                    yield spoken
            # Spec M3 (T6b-1): the final chunk carries usage (StreamChunk.usage);
            # capture this round's real token usage for the served LLM's meter,
            # summed across the turn's rounds (a tool round + re-prompt).
            if chunk.usage is not None and self._turn_meter is not None:
                self._turn_meter.note_llm_usage(
                    prompt_tokens=chunk.usage.prompt_tokens,
                    completion_tokens=chunk.usage.completion_tokens,
                    cost_usd=chunk.usage.cost_usd,
                    provider=backend.provider_name,
                    model=backend.model_name,
                )
            tcd = chunk.tool_call_delta
            if tcd is not None:
                if tcd.call_id not in names:
                    order.append(tcd.call_id)
                    names[tcd.call_id] = ""
                    args_json[tcd.call_id] = ""
                names[tcd.call_id] += tcd.name_delta
                args_json[tcd.call_id] += tcd.arguments_delta
        spoken_tail = strip_converter.flush()
        if spoken_tail:
            if self._turn_meter is not None:
                self._turn_meter.note_tts_chars(len(spoken_tail))
            yield spoken_tail
        calls_out.extend(self._build_call(cid, names[cid], args_json[cid]) for cid in order)

    def _capture_stance(self) -> Callable[[str], None] | None:
        """The converter's ``on_feeling`` callback: publish the utterance's stance (V12-D-2).

        Returns ``None`` when no expressivity listener is wired (⇒ pure STRIP, byte-identical
        to N5). Otherwise returns a callback that, on the **first** recognised feeling-tag of
        this round (lead-with-the-tag), resolves it to a :class:`VoiceExpressivity` and hands
        it to the listener. Fires once per round; the TTS side reads the pending value once at
        the first send (per-context consistency), so the effective config is the first tag
        seen before the first spoken chunk.

        **Fail-safe (V12-D-5):** this runs inside the streaming converter, so it MUST NOT
        raise — any error resolving/publishing is swallowed (logged), leaving the spoken
        stream untouched and the delivery flat. Expressivity can only ever add, never break.
        """
        listener = self._expressivity_listener
        if listener is None:
            return None
        published = False

        def _on_feeling(name: str) -> None:
            nonlocal published
            if published:
                return
            published = True
            try:
                listener(resolve_expressivity([name]))
            except Exception:  # noqa: BLE001 — never let expressivity break the spoken stream
                _logger.warning("voice expressivity publish failed; delivery stays flat")

        return _on_feeling

    def _followup_prompt(
        self,
        backend: ChatBackend,
        base_prompt: list[ConversationMessage],
        round_text: list[str],
        call: ToolCall,
        result: object,
    ) -> list[ConversationMessage]:
        """Build the re-prompt carrying the tool result (one round; mirrors the loop)."""
        from persona.schema.tools import ToolResult

        assert isinstance(result, ToolResult)
        messages = list(base_prompt)
        if backend.supports_native_tools:
            # Native providers require the assistant tool_calls message before the
            # tool result (Spec 11 soak finding).
            messages.append(
                ConversationMessage(
                    role="assistant",
                    content="".join(round_text),
                    created_at=datetime.now(UTC),
                    tool_calls=[call],
                )
            )
        messages.append(format_tool_result(call, result, provider_name=backend.provider_name))
        return messages

    def _offered_specs(self, backend: ChatBackend) -> list[ToolSpec] | None:  # noqa: ARG002 — backend reserved for future per-provider gating
        """The voice-offered tool specs (viable + deferred), or ``None`` if none."""
        if self._ctx.toolbox is None:
            return None
        specs = self._tool_policy.offered_specs(self._ctx.toolbox.get_specs())
        return specs or None

    def _routing_context(self, user_message: str) -> RoutingContext:
        """Build this turn's routing context (voice: never an image turn).

        Reuses the public routing classifiers (D-V5-6 — routing mechanics compose
        the shared parts; this is not conditioning, so it is not the shared
        retrieval/prompt path). Mirrors the text loop's context shape.
        """
        conversation = self._ctx.conversation
        is_first = conversation.turn_count == 0
        return RoutingContext(
            requires_vision=False,
            estimated_input_tokens=len(user_message) // 4,
            requires_strong_tools=False,
            is_first_turn=is_first,
            is_identity_sensitive=classifiers.is_persona_critical(user_message, self._ctx.persona),
            is_boilerplate=classifiers.is_boilerplate(user_message),
            conversation_phase="opening" if is_first else "middle",
            # Spec P9 (P9-D-3): the VOICE profile — the policy resolves the named
            # LATENCY tier. The reason is the turn-taking budget (800 ms P50 /
            # 1.5 s P95 end-to-end, R-V1-3), NOT cost: measured 2026-07-04
            # (P9-R-2), no configured frontier model's first token fits the
            # budget's model hop (GLM 5.2 median TTFT 6 565 ms, sonnet-4-6
            # 857 ms, vs the mid primary's 57 ms). A future fast-enough frontier
            # swaps in via PERSONA_MID_MODELS (ops) — no code change here.
            profile="voice",
        )

    def _choose_tier(self, routing_context: RoutingContext) -> str:
        """Pick the tier — persona pin wins (a deliberate override, P9-D-7),
        else the policy router: the voice profile resolves the latency tier
        (P9-D-3 — see the ``profile="voice"`` note in :meth:`_routing_context`)."""
        override = self._ctx.persona.routing.tier_for_generation
        if override != "auto":
            return override
        return self._ctx.router.route(routing_context).tier

    @staticmethod
    def _build_call(call_id: str, name: str, raw_args: str) -> ToolCall:
        """Reconstruct a ToolCall from streamed deltas (fail-safe args parse)."""
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            args = {}  # the @tool decorator validates downstream
        if not isinstance(args, dict):
            args = {}
        return ToolCall(name=name, args=args, call_id=call_id)

    @staticmethod
    def _max_tokens(backend: object) -> int:
        """Best-effort prompt-window budget (mirrors the text loop's helper)."""
        value = getattr(backend, "max_tokens", None)
        return value if isinstance(value, int) and value > 0 else _DEFAULT_MAX_TOKENS
