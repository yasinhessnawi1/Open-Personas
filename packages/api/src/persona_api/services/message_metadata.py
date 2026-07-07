"""Runtime message-metadata persistence over the ``channel`` JSON column (R4 rail fix).

The pending-proposal rails (A4 ``contract_proposal``, A5 ``cancel_proposal``, A8
``reschedule_proposal``) and Spec 21's ``proactive_question`` marker all ride
``ConversationMessage.metadata`` — which, before this module, existed ONLY in memory: the
``messages`` table had no metadata home on the chat path, so every rail lost its pending
state the moment the turn ended and the next turn reloaded the conversation from the DB
(the R4 operator-pass escape: "once at 9 30" ran as a free model turn and confabulated an
``.ics`` instead of amending the pending proposal).

**One message-attached JSON mechanism, not two** (the A9 alignment): Spec A9's delegated
turn already persists message-attached data in the existing ``messages.channel`` JSON
column (``delegation_outcome`` / ``delegation_key``, read back via
``channel->>'delegation_key'``). This module extends that same column rather than adding a
parallel one: the loop's final assistant metadata is namespaced under
``channel["runtime_metadata"]`` (never colliding with A9's top-level keys or the user row's
``ChannelContext`` shape), written at finalize and rehydrated at load. Additive + nullable
— every historical row and every metadata-free turn round-trips byte-identically.
"""

from __future__ import annotations

__all__ = [
    "RUNTIME_METADATA_KEY",
    "channel_payload_for_metadata",
    "metadata_from_channel",
]

#: The namespace inside the ``channel`` JSON that carries the runtime message metadata.
RUNTIME_METADATA_KEY = "runtime_metadata"


def channel_payload_for_metadata(metadata: dict[str, str]) -> dict[str, object] | None:
    """The ``channel`` column value carrying ``metadata``, or ``None`` when there is nothing.

    ``None`` (not ``{}``) keeps the metadata-free path byte-identical: the assistant row's
    ``channel`` stays NULL exactly as before, so historical dumps and the community DB are
    unchanged for every ordinary turn.
    """
    if not metadata:
        return None
    return {RUNTIME_METADATA_KEY: dict(metadata)}


def metadata_from_channel(channel: object) -> dict[str, str]:
    """Rehydrate the runtime metadata from a loaded ``channel`` value (fail-soft).

    Anything that is not the namespaced string→string dict yields ``{}`` — a legacy row, a
    connector ``ChannelContext``, or an A9 delegation channel all degrade cleanly to the
    pre-fix shape rather than erroring a conversation load.
    """
    if not isinstance(channel, dict):
        return {}
    raw = channel.get(RUNTIME_METADATA_KEY)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
