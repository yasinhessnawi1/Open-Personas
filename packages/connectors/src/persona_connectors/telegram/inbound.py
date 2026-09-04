"""Inbound normalisation — a Telegram ``Update`` → C1's shape (Spec C2 T2).

The adapter's job at the front of the flow: take Telegram's raw ``Update`` JSON and
classify it into one of three pure outcomes the shared flow (T7) branches on —

- :class:`InboundText` — a text message, carried as a C1
  :class:`~persona_connectors.domain.normalise.NormalisedInbound` (the only shape
  the framework consumes). A command (``/start``, ``/new``) is still *text*; the
  command router (a later task) inspects ``.text`` — keeping this step thin and
  transport-only.
- :class:`InboundNonText` — voice / media / unsupported content, classified to a
  small :class:`NonTextKind` so T4 can render the friendly text-only decline
  (D-C2-6); it carries just enough (``conversation_key`` / ``message_id``) to
  reply, never driving a runtime turn.
- :class:`InboundIgnore` — a non-message update (edited message, channel post,
  reaction, …) or a service message (group join/leave/pin); silently skipped, no
  reply.

**The two facts research flagged (C2-R-1):** Telegram's ``date`` is a Unix
timestamp → converted to a **tz-aware UTC** ``datetime`` (the project's
everywhere-aware rule; ingestion-time fallback when absent); the **sender/chat
mapping** is ``from.id`` → ``sender_id`` (the identity key that drives BOTH linking
and resolution, C1-D-5) and ``chat.id`` → ``conversation_key`` (the channel key).

This module is **pure + api-free**: deterministic over its input (``now`` injected
for the fallback), no I/O, no ``persona_api`` — unit-tested exhaustively.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from persona_connectors.domain.normalise import NormalisedInbound

__all__ = [
    "PLATFORM",
    "InboundIgnore",
    "InboundNonText",
    "InboundText",
    "NonTextKind",
    "NormalisedUpdate",
    "classify_update",
]

# The opaque platform key carried on every NormalisedInbound + the DeliveryRouter
# registration key (D-08-3 — never branched on by the framework).
PLATFORM = "telegram"

# Telegram content fields that mean "non-text user content" (criterion 8 / D-C2-6).
_VOICE_KEYS = frozenset({"voice", "video_note"})
_MEDIA_KEYS = frozenset({"photo", "video", "document", "sticker", "audio", "animation"})
# Service / system messages (mostly group lifecycle — groups are out of scope, §2):
# silently ignored rather than declined, so the bot never replies to system noise.
_SERVICE_KEYS = frozenset(
    {
        "new_chat_members",
        "left_chat_member",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "message_auto_delete_timer_changed",
        "pinned_message",
        "migrate_to_chat_id",
        "migrate_from_chat_id",
    }
)


class NonTextKind(StrEnum):
    """The class of non-text content, driving the friendly decline (D-C2-6).

    Coarse on purpose: ``voice`` (can't listen yet), ``media`` (photo/video/doc/
    sticker/…), and ``unknown`` (any other unsupported content) — T4 maps each to a
    product-voice line; the framework is text-only in v1.
    """

    voice = "voice"
    media = "media"
    unknown = "unknown"


class InboundText(BaseModel):
    """A text message normalised to C1's inbound shape — drives the shared flow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inbound: NormalisedInbound


class InboundNonText(BaseModel):
    """Non-text content — declined gracefully (D-C2-6), never a runtime turn.

    Carries only what a decline reply needs: where to send it
    (``conversation_key``), the message to reply to (``message_id``), and the kind.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: NonTextKind
    conversation_key: str
    sender_id: str
    message_id: str


class InboundIgnore(BaseModel):
    """An update with nothing to act on — silently skipped (no reply).

    Attributes:
        reason: A short tag for observability (``"non-message-update"`` /
            ``"service-message"`` / ``"empty-message"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str


# The three outcomes the flow branches on.
NormalisedUpdate = InboundText | InboundNonText | InboundIgnore


def _as_dict(value: object) -> dict[str, object] | None:
    """Narrow a JSON value to an object, else ``None`` (defensive over raw payloads)."""
    return value if isinstance(value, dict) else None


def _id_str(value: object) -> str | None:
    """Stringify a Telegram numeric/string id; ``None`` if absent or wrong-typed."""
    if isinstance(value, bool):  # bool is an int subclass — never an id
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return None


def _received_at(message: dict[str, object], *, fallback: datetime) -> datetime:
    """Convert Telegram's Unix ``date`` to tz-aware UTC (ingestion-time fallback)."""
    date = message.get("date")
    if isinstance(date, int) and not isinstance(date, bool):
        return datetime.fromtimestamp(date, tz=UTC)
    return fallback


def _display_name(sender: dict[str, object]) -> str | None:
    """Build a sender display name from first/last name, else @username, else None."""
    first = sender.get("first_name")
    last = sender.get("last_name")
    parts = [p for p in (first, last) if isinstance(p, str) and p]
    if parts:
        return " ".join(parts)
    username = sender.get("username")
    return username if isinstance(username, str) and username else None


def _reply_to_id(message: dict[str, object]) -> str | None:
    """Extract the replied-to message id, if this message quotes another."""
    replied = _as_dict(message.get("reply_to_message"))
    return _id_str(replied.get("message_id")) if replied is not None else None


def _non_text_kind(message: dict[str, object]) -> NonTextKind | None:
    """Classify a message's non-text content, or ``None`` if it's a service message."""
    if any(key in message for key in _VOICE_KEYS):
        return NonTextKind.voice
    if any(key in message for key in _MEDIA_KEYS):
        return NonTextKind.media
    if any(key in message for key in _SERVICE_KEYS):
        return None  # service / system message → ignore (no decline)
    return NonTextKind.unknown


def classify_update(update: dict[str, object], *, now: datetime) -> NormalisedUpdate:
    """Classify a raw Telegram ``Update`` into a :data:`NormalisedUpdate` (pure).

    Args:
        update: The decoded Telegram ``Update`` JSON object.
        now: Tz-aware UTC ingestion time — the fallback when a message has no
            usable ``date`` (the everywhere-aware rule; injected so this is pure).

    Returns:
        :class:`InboundText` for a text message (the C1 ``NormalisedInbound``),
        :class:`InboundNonText` for voice/media/unsupported content, or
        :class:`InboundIgnore` for a non-message / service / malformed update.
    """
    message = _as_dict(update.get("message"))
    if message is None:
        # edited_message / channel_post / callback_query / reaction / … — not a
        # direct user message in the 1:1 text model (allowed_updates narrows this,
        # but we stay defensive).
        return InboundIgnore(reason="non-message-update")

    sender = _as_dict(message.get("from"))
    chat = _as_dict(message.get("chat"))
    sender_id = _id_str(sender.get("id")) if sender is not None else None
    conversation_key = _id_str(chat.get("id")) if chat is not None else None
    message_id = _id_str(message.get("message_id"))
    if sender_id is None or conversation_key is None or message_id is None:
        return InboundIgnore(reason="malformed-message")

    text = message.get("text")
    if isinstance(text, str):
        raw: dict[str, str] = {"platform": PLATFORM}
        update_id = _id_str(update.get("update_id"))
        if update_id is not None:
            raw["update_id"] = update_id
        if chat is not None and isinstance(chat.get("type"), str):
            raw["chat_type"] = str(chat["type"])
        inbound = NormalisedInbound(
            platform=PLATFORM,
            sender_id=sender_id,
            conversation_key=conversation_key,
            message_id=message_id,
            text=text,
            received_at=_received_at(message, fallback=now),
            reply_to_message_id=_reply_to_id(message),
            display_name=_display_name(sender) if sender is not None else None,
            raw=raw,
        )
        return InboundText(inbound=inbound)

    kind = _non_text_kind(message)
    if kind is None:
        return InboundIgnore(reason="service-message")
    if kind is NonTextKind.unknown:
        # R9-082: a message whose content we cannot name is NOT the user talking to us,
        # and answering it is what put "I work over text -- send me a message and I'll
        # reply." in front of every message in the owner's chat: Telegram delivered a
        # message with unrecognised content ~0.24s BEFORE the text, the flow declined it,
        # then the real text arrived. A decline is right only when the user clearly sent
        # something we cannot use (a voice note, a photo); for content we cannot even
        # classify, the honest behaviour is the same as for a service message: log why,
        # say nothing. The decline copy was also wrong for the case, since the user had
        # sent text and was about to be told to send text.
        return InboundIgnore(reason="unrecognised-content")
    return InboundNonText(
        kind=kind,
        conversation_key=conversation_key,
        sender_id=sender_id,
        message_id=message_id,
    )
