"""Inbound normalisation — a Twilio WhatsApp form POST → C1's shape (Spec C4 T4).

The adapter's job at the front of the flow: take Twilio's form-encoded WhatsApp
inbound and classify it into one of three pure outcomes the shared flow branches on —

- :class:`InboundText` — a text message, carried as a C1
  :class:`~persona_connectors.domain.normalise.NormalisedInbound`. A command (``/new``)
  is still *text*; the shared flow's command parser inspects ``.text``.
- :class:`InboundNonText` — voice / media / unsupported content (Twilio sets
  ``NumMedia > 0`` + ``MediaContentType0``), classified to a small :class:`NonTextKind`
  so the flow renders the friendly text-only decline (D-C2-6 carried forward).
- :class:`InboundIgnore` — an empty/whitespace body with no media, or a malformed
  payload (no ``MessageSid`` / ``From``); silently skipped, no reply.

**The identity mapping (D-C1-5):** Twilio's ``From`` is ``whatsapp:+E164`` — the
``whatsapp:`` prefix is **stripped** so the ``sender_id`` / ``conversation_key`` is the
bare E.164 number (the identity key that drives BOTH linking and resolution, shared
with the SMS adapter's bare-E.164 identity; only the ``platform`` distinguishes them).
``MessageSid`` is the per-message id. Twilio carries no inbound timestamp, so
``received_at`` is the injected ingestion-time (tz-aware UTC — the everywhere-aware rule).

**Non-text taxonomy** mirrors Telegram's ``{voice, media, unknown}`` (WhatsApp DOES have
a voice primitive — an ``audio/*`` media type — unlike Slack): ``audio/*`` → voice,
any other media → media, media without a classifiable type → unknown.

Pure + api-free: deterministic over its input (``now`` injected), no I/O, no ``persona_api``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from persona_connectors.domain.normalise import NormalisedInbound

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

__all__ = [
    "PLATFORM",
    "WHATSAPP_PREFIX",
    "InboundIgnore",
    "InboundNonText",
    "InboundText",
    "NonTextKind",
    "NormalisedInboundMessage",
    "classify_inbound",
]

# The opaque platform key carried on every NormalisedInbound + the DeliveryRouter
# registration key (D-08-3 — never branched on by the framework).
PLATFORM = "whatsapp"

# Twilio prefixes WhatsApp addresses with ``whatsapp:`` (``whatsapp:+E164``); stripped
# so the identity is the bare E.164 number (shared identity shape with SMS).
WHATSAPP_PREFIX = "whatsapp:"


class NonTextKind(StrEnum):
    """The class of non-text content, driving the friendly decline (D-C2-6 carried forward).

    Mirrors Telegram's taxonomy (WhatsApp has a real voice primitive — an ``audio/*``
    media type): ``voice`` (can't listen yet), ``media`` (image/video/document/…), and
    ``unknown`` (media without a classifiable content type). The flow maps each to a
    product-voice decline line; the framework is text-only in v1.
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

    Carries only what a decline reply needs: where to send it (``conversation_key``),
    the message to reply to (``message_id``), the sender, and the kind.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: NonTextKind
    conversation_key: str
    sender_id: str
    message_id: str


class InboundIgnore(BaseModel):
    """An inbound with nothing to act on — silently skipped (no reply).

    Attributes:
        reason: A short tag for observability (``"malformed"`` / ``"empty"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str


# The three outcomes the flow branches on.
NormalisedInboundMessage = InboundText | InboundNonText | InboundIgnore


def _strip_prefix(address: str) -> str:
    """Strip Twilio's ``whatsapp:`` prefix → the bare E.164 identity."""
    if address.startswith(WHATSAPP_PREFIX):
        return address[len(WHATSAPP_PREFIX) :]
    return address


def _num_media(form: Mapping[str, str]) -> int:
    """Parse Twilio's ``NumMedia`` count (defensive — defaults to 0)."""
    raw = form.get("NumMedia", "0")
    try:
        return int(raw)
    except ValueError:
        return 0


def _non_text_kind(form: Mapping[str, str]) -> NonTextKind:
    """Classify a media inbound by its first media content type."""
    content_type = form.get("MediaContentType0", "")
    if content_type.startswith("audio/"):
        return NonTextKind.voice
    if content_type:
        return NonTextKind.media
    return NonTextKind.unknown


def classify_inbound(form: Mapping[str, str], *, now: datetime) -> NormalisedInboundMessage:
    """Classify a raw Twilio WhatsApp inbound form into a :data:`NormalisedInboundMessage`.

    Args:
        form: The decoded Twilio inbound form params (``From`` / ``To`` / ``Body`` /
            ``MessageSid`` / ``NumMedia`` / ``MediaContentType0`` / …).
        now: Tz-aware UTC ingestion time — the ``received_at`` (Twilio carries no inbound
            timestamp; injected so this is pure).

    Returns:
        :class:`InboundText` for a text message (the C1 ``NormalisedInbound``),
        :class:`InboundNonText` for voice/media/unsupported content, or
        :class:`InboundIgnore` for an empty / malformed inbound.
    """
    message_sid = form.get("MessageSid")
    sender_raw = form.get("From")
    if not message_sid or not sender_raw:
        return InboundIgnore(reason="malformed")

    sender_id = _strip_prefix(sender_raw)
    num_media = _num_media(form)

    # A media inbound is non-text even with a caption (the C2 photo-caption rule).
    if num_media > 0:
        return InboundNonText(
            kind=_non_text_kind(form),
            conversation_key=sender_id,
            sender_id=sender_id,
            message_id=message_sid,
        )

    body = form.get("Body", "")
    if body.strip():
        raw: dict[str, str] = {"platform": PLATFORM}
        to = form.get("To")
        if to:
            raw["to"] = _strip_prefix(to)
        inbound = NormalisedInbound(
            platform=PLATFORM,
            sender_id=sender_id,
            conversation_key=sender_id,
            message_id=message_sid,
            text=body,
            received_at=now,
            raw=raw,
        )
        return InboundText(inbound=inbound)

    return InboundIgnore(reason="empty")
