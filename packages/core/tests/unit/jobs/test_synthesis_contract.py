"""The canonical synthesis-job contract (Spec V13, D-4-amended).

``persona.jobs`` owns the ONE definition of a synthesis job so the api writer and
the voice writer cannot drift on payload shape, idempotency key, or the ``channel``
provenance marker. These pin that contract.
"""

from __future__ import annotations

from persona.extraction import InteractionKind
from persona.jobs import (
    CHANNEL_CHAT,
    CHANNEL_VOICE,
    SYNTHESIS_JOB_TYPE,
    SynthesisJobPayload,
    make_conversation_synthesis_payload,
    synthesis_idempotency_key,
)


def test_channel_defaults_to_chat_for_backward_compatibility() -> None:
    # A payload reconstructed from a pre-V13 stored dict (no ``channel``) defaults to
    # ``chat`` — old jobs in the queue reconstruct unchanged.
    payload = SynthesisJobPayload(
        interaction_kind="conversation",
        interaction_id="c1",
        persona_id="p1",
        high_water_mark=4,
    )
    assert payload.channel == CHANNEL_CHAT


def test_voice_payload_carries_the_voice_channel() -> None:
    payload = make_conversation_synthesis_payload(
        conversation_id="call-1", persona_id="p1", message_count=6, channel=CHANNEL_VOICE
    )
    assert payload.channel == CHANNEL_VOICE
    # Voice persists to the messages table like chat → interaction_kind is conversation,
    # so the existing synthesis repository reads it; ``channel`` is the only difference.
    assert payload.interaction_kind == InteractionKind.CONVERSATION.value


def test_idempotency_key_excludes_channel_so_voice_and_chat_share_the_key_space() -> None:
    # The key must NOT include channel: interaction_id already partitions a call from a
    # chat, and adding channel would let one interaction enqueue twice.
    voice = make_conversation_synthesis_payload(
        conversation_id="c1", persona_id="p1", message_count=3, channel=CHANNEL_VOICE
    )
    chat = make_conversation_synthesis_payload(
        conversation_id="c1", persona_id="p1", message_count=3, channel=CHANNEL_CHAT
    )
    assert synthesis_idempotency_key(voice) == synthesis_idempotency_key(chat)
    assert synthesis_idempotency_key(voice) == "synthesis:conversation:c1:3"


def test_job_type_is_the_shared_constant() -> None:
    assert SYNTHESIS_JOB_TYPE == "synthesis"
