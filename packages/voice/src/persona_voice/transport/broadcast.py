"""DataChannelBroadcaster — state + caption broadcast to the browser (spec V6 A1).

The V6-owned backend seam (D-V6-X-agent-worker) that makes V4's conversational
states + V2/V5 transcripts *legible in the browser*: it serializes them to a
single discriminated JSON envelope (D-V6-E1) and publishes them over the LiveKit
data channel via :meth:`VoiceRoom.publish_data` — the same peer connection the
audio rides. The orchestrator already fires a
:class:`~persona_voice.turn_taking.orchestrator.ConversationalStateListener`
seam for state; the loop fires a
:class:`~persona_voice.loop.streaming.VoiceCaptionListener` seam for captions;
this one object implements BOTH so a single broadcaster serves the call.

**Envelope (D-V6-E1 / D-V6-E2).** One topic (``persona-voice``), reliable +
ordered (a dropped ``thinking→speaking`` transition would desync the client's
state visualisation). Two frame types under a ``type`` discriminator:

* ``{"type":"state", "from_state","to_state","trigger","at"}`` — a
  :class:`ConversationalTransition` (V6 renders the orb from ``to_state``;
  reflects barge-in from ``trigger=="barge_in"``).
* ``{"type":"transcript", "speaker","text","is_final","segment_id"}`` — one
  caption segment. ``speaker`` ∈ {``user``, ``persona``}; ``segment_id`` lets the
  client mutate-and-replace the current partial in place (D-V6-2 anti-flicker)
  and a new id marks a new utterance after a final.
* **V10-D-6 rich-output frames** — ``{"type": <RunEvent.type>, **RunEvent.data}``
  for ``tool_calling`` / ``tool_result`` (carrying ``artifacts`` in the SAME
  shape chat uses — ``workspace_path``/``mime_type``/``size_bytes``/
  ``rendered_inline`` — so the web normaliser + ``FileRendererPanel`` render it
  unchanged) / ``activity_start`` / ``activity_end`` (the live "using <X>…"
  badge). One event vocabulary across chat + voice; only the transport differs
  (this data channel vs chat's SSE). The render frame is the SAME ``tool_result``
  chat carries the artifact on (P2-D-3 keep-both). Emitted via
  :meth:`DataChannelBroadcaster.on_run_event` — a thin reuse of the existing
  reliable+ordered publish (NOT a new per-turn blocking path; the async lane that
  drives most of these runs off-thread, V10-R-2).

**Owner-only delivery.** :meth:`VoiceRoom.publish_data` passes no
``destination_identities``, so LiveKit confines the frame to the per-session
Room's participants — the call's own authenticated owner (+ this agent). See
that method's docstring for the room-scoping argument.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from persona.logging import get_logger

if TYPE_CHECKING:
    from persona_runtime.agentic.events import RunEvent

    from persona_voice.loop.streaming import Transcript
    from persona_voice.transport.room import VoiceRoom
    from persona_voice.turn_taking.states import ConversationalTransition

__all__ = [
    "BROADCAST_TOPIC",
    "DataChannelBroadcaster",
    "encode_run_event_frame",
    "encode_state_frame",
    "encode_transcript_frame",
]

_logger = get_logger("transport.broadcast")

# The single data-channel topic the browser subscribes to (D-V6-E1). One topic,
# one discriminated envelope, one client decoder (mirrors lib/sse-types.ts).
BROADCAST_TOPIC = "persona-voice"


def encode_state_frame(transition: ConversationalTransition) -> bytes:
    """Serialize a conversational-state transition to the wire envelope."""
    return json.dumps(
        {
            "type": "state",
            "from_state": str(transition.from_state),
            "to_state": str(transition.to_state),
            "trigger": str(transition.trigger),
            "at": transition.at.isoformat(),
        }
    ).encode("utf-8")


def encode_transcript_frame(*, speaker: str, text: str, is_final: bool, segment_id: str) -> bytes:
    """Serialize one caption segment to the wire envelope."""
    return json.dumps(
        {
            "type": "transcript",
            "speaker": speaker,
            "text": text,
            "is_final": is_final,
            "segment_id": segment_id,
        }
    ).encode("utf-8")


def encode_run_event_frame(event: RunEvent) -> bytes:
    """Serialize a rich-output ``RunEvent`` to the wire envelope (V10-D-6).

    Flat ``{"type": event.type, **event.data}`` — the event's ``type`` is the
    client discriminator and its already-JSON-safe ``data`` (built by the typed
    ``RunEvent`` constructors) carries the payload verbatim, so the voice client
    decodes ``tool_result``/``activity_*`` exactly as the chat client decodes the
    same ``RunEvent`` over SSE. No voice-only reshaping — one vocabulary.
    """
    return json.dumps({"type": event.type, **event.data}).encode("utf-8")


class DataChannelBroadcaster:
    """Publishes state transitions + caption segments to the browser.

    Implements both the V4 ``ConversationalStateListener`` (``on_state_changed``)
    and the V6/A1 ``VoiceCaptionListener`` (``on_user_transcript`` /
    ``on_persona_text``) seams. Per the listener contract these methods MUST NOT
    raise — a transient data-channel publish error is caught + logged so it never
    corrupts the live turn cycle (a dropped caption frame is cosmetic; a raised
    exception would wedge the orchestrator).

    Segment ids: ``u{n}`` / ``p{n}`` per speaker, incremented after each final so
    a new utterance starts a fresh client segment while partials of the current
    utterance share one id (the mutate-and-replace target — D-V6-2).
    """

    def __init__(self, voice_room: VoiceRoom, *, topic: str = BROADCAST_TOPIC) -> None:
        self._room = voice_room
        self._topic = topic
        self._user_segment = 0
        self._persona_segment = 0
        # R9-052: set once we've warned about a disconnected Room so the flood
        # of still-live listener callbacks firing after teardown warns exactly
        # once, not per frame. Reset whenever the Room is observed connected.
        self._publish_suppressed = False

    async def on_state_changed(self, transition: ConversationalTransition) -> None:
        """V4 ``ConversationalStateListener`` — broadcast the state transition."""
        await self._publish(encode_state_frame(transition))

    async def on_user_transcript(self, transcript: Transcript) -> None:
        """V6/A1 ``VoiceCaptionListener`` — broadcast a user caption segment."""
        await self._publish(
            encode_transcript_frame(
                speaker="user",
                text=transcript.text,
                is_final=transcript.is_final,
                segment_id=f"u{self._user_segment}",
            )
        )
        if transcript.is_final:
            self._user_segment += 1

    async def on_persona_text(self, text: str, *, is_final: bool) -> None:
        """V6/A1 ``VoiceCaptionListener`` — broadcast a persona caption segment."""
        await self._publish(
            encode_transcript_frame(
                speaker="persona",
                text=text,
                is_final=is_final,
                segment_id=f"p{self._persona_segment}",
            )
        )
        if is_final:
            self._persona_segment += 1

    async def on_run_event(self, event: RunEvent) -> None:
        """V10-D-6 rich-output sink — broadcast an activity/tool ``RunEvent`` frame.

        The ``on_event`` sink the producer's inline tool dispatch and the async
        artifact lane push to: ``activity_start``/``activity_end`` (the "using
        <X>…" badge) and ``tool_result`` (the artifact the ``FileRendererPanel``
        renders). Reuses the same reliable+ordered publish + never-raise contract
        as the caption/state frames; the lane drives most of these off-thread, so
        this adds no per-turn blocking on the talk path (V10-R-2 / the V5 latency
        bound).
        """
        await self._publish(encode_run_event_frame(event))

    async def _publish(self, payload: bytes) -> None:
        """Publish one reliable frame; swallow + log transport errors (never raise).

        Gated on Room liveness (R9-052). Room teardown (e.g. a billing cutoff
        calling ``delete_room``) doesn't instantly stop the still-live caption /
        state / run-event listeners — they keep firing for the last few frames
        of the turn before their owning tasks are cancelled, and each one used
        to hit the ``except`` below and warn, flooding the log (hundreds of
        identical lines per teardown). While the Room is disconnected we skip
        the publish silently, warning exactly ONCE on the connected→disconnected
        transition. Observing the Room connected again resets the flag, so a
        later reconnect's genuine error still surfaces.
        """
        if not self._room.is_connected:
            if not self._publish_suppressed:
                self._publish_suppressed = True
                _logger.warning(
                    "voice data-channel closed; suppressing further publishes (topic={topic})",
                    topic=self._topic,
                )
            return
        self._publish_suppressed = False
        try:
            await self._room.publish_data(payload, reliable=True, topic=self._topic)
        except Exception:  # noqa: BLE001 — listener contract: never raise into the turn
            _logger.warning("voice data-channel publish failed (topic={topic})", topic=self._topic)
