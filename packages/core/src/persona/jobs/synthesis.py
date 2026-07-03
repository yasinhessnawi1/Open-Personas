"""The synthesis-job contract — the ONE definition both writers share (Spec V13, D-4-amended).

The durable ``synthesis`` job is enqueued from two processes: persona-api (a web
turn-end / agentic-run-end, via ``JobQueue.enqueue``) and persona-voice (a call
session-end, via a small raw INSERT beside its other raw-SQL peers). Refined
Option B keeps the *table* api-owned but collapses the **semantic** drift surface
to a single definition here in core — the payload shape, the idempotency key, the
job-type string, and the ``channel`` provenance marker — so the two thin writers
cannot disagree about *what* a synthesis job is, only about *how* they INSERT the
row (pinned by a bidirectional column-parity test + the real-transition test).

``persona.jobs`` already owns the job domain (``JobPayload``) and this is domain,
not persistence: no SQL, no table knowledge — exactly the "shared truth that keeps
the two execution paths from drifting" this package exists for.
"""

from __future__ import annotations

from persona.jobs.models import JobPayload

__all__ = [
    "CHANNEL_CHAT",
    "CHANNEL_VOICE",
    "SYNTHESIS_JOB_TYPE",
    "SynthesisJobPayload",
    "make_conversation_synthesis_payload",
    "synthesis_idempotency_key",
]

#: The durable job type string (the A0 ``jobs.type`` value + registry key).
SYNTHESIS_JOB_TYPE = "synthesis"

#: The ``channel`` provenance markers. ``source`` stays ``WriteSource.SYSTEM`` for
#: all synthesis (it is a system reflection pass); ``channel`` records which surface
#: the interaction came from, so a graph fact minted from a call is attributable
#: (``source: voice``) without changing ``interaction_kind`` (voice persists as a
#: ``conversation``, which is what the synthesis repository reads).
CHANNEL_CHAT = "chat"
CHANNEL_VOICE = "voice"


class SynthesisJobPayload(JobPayload):
    """Which completed interaction to synthesise (frozen + ``extra='forbid'``).

    ``high_water_mark`` is the enqueue-time message count — it scopes the A0
    idempotency key so a continued conversation re-keys to a new job (D-K2-2). The
    handler re-reads the live marker + messages at run time; it does not trust this
    value for the actual windowing.

    ``channel`` (V13 D-4-amended) marks the originating surface for provenance
    (``chat`` default; ``voice`` for a call). It does NOT enter the idempotency key
    (a call and a chat never share a ``conversation_id``) and it defaults to
    ``chat`` so payloads enqueued before V13 reconstruct unchanged.

    Attributes:
        interaction_kind: The synthesis source kind (``conversation`` for both web
            and voice, since voice persists to the same messages table).
        interaction_id: The source interaction id (the conversation id).
        persona_id: The persona whose interaction this is.
        high_water_mark: The enqueue-time message count (idempotency-key scope).
        channel: The originating surface (``chat`` / ``voice``) — provenance only.
    """

    interaction_kind: str
    interaction_id: str
    persona_id: str
    high_water_mark: int
    channel: str = CHANNEL_CHAT


def synthesis_idempotency_key(payload: SynthesisJobPayload) -> str:
    """``synthesis:{kind}:{interaction_id}:{high_water_mark}`` (D-K2-2).

    Deliberately excludes ``channel``: the ``interaction_id`` already partitions
    voice (a call's conversation id) from chat, so the channel would add nothing but
    a spurious way for the same interaction to enqueue twice.
    """
    return (
        f"synthesis:{payload.interaction_kind}:{payload.interaction_id}:{payload.high_water_mark}"
    )


def make_conversation_synthesis_payload(
    *,
    conversation_id: str,
    persona_id: str,
    message_count: int,
    channel: str = CHANNEL_CHAT,
) -> SynthesisJobPayload:
    """Build the payload for a conversation-boundary synthesis (web or voice).

    The single ``channel → payload`` mapping both writers call, so the api turn-end
    and the voice session-end produce byte-identical payloads modulo ``channel``.
    ``interaction_kind`` is always ``conversation`` (voice persists there too).
    """
    from persona.extraction import InteractionKind

    return SynthesisJobPayload(
        interaction_kind=InteractionKind.CONVERSATION.value,
        interaction_id=conversation_id,
        persona_id=persona_id,
        high_water_mark=message_count,
        channel=channel,
    )
