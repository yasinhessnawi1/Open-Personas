"""Inbound normalisation — a Twilio SMS form POST → C1's shape (Spec C4 T4).

The adapter's job at the front of the flow: take Twilio's form-encoded SMS inbound and
classify it into one of three pure outcomes the shared flow branches on —

- :class:`InboundText` — a text SMS, carried as a C1
  :class:`~persona_connectors.domain.normalise.NormalisedInbound`. A command (``/new``)
  is still *text*; the shared flow's command parser inspects ``.text``.
- :class:`InboundNonText` — an MMS (``NumMedia > 0``), classified to a small
  :class:`NonTextKind` so the flow renders the friendly text-only decline.
- :class:`InboundIgnore` — an empty/whitespace body with no media, or a malformed
  payload (no ``MessageSid`` / ``From``); silently skipped, no reply.

**The identity mapping (D-C1-5):** Twilio's ``From`` is a bare ``+E164`` for SMS (no
prefix to strip — unlike WhatsApp) — the ``sender_id`` / ``conversation_key`` is that
number (the key that drives BOTH linking and resolution; only the ``platform``
distinguishes SMS from WhatsApp, which share the bare-E.164 identity shape).
``MessageSid`` is the per-message id. Twilio carries no inbound timestamp, so
``received_at`` is the injected ingestion-time (tz-aware UTC — the everywhere-aware rule).

**Non-text taxonomy is SMS-LOCAL: just ``media`` + ``unknown``** (SMS/MMS has no voice
primitive — an MMS is a media attachment, the rule-of-three divergence from WhatsApp).

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
    "InboundIgnore",
    "InboundNonText",
    "InboundText",
    "NonTextKind",
    "NormalisedInboundMessage",
    "classify_inbound",
]

# The opaque platform key carried on every NormalisedInbound + the DeliveryRouter
# registration key (D-08-3 — never branched on by the framework).
PLATFORM = "sms"


class NonTextKind(StrEnum):
    """The class of non-text content, driving the friendly decline (D-C2-6 carried forward).

    SMS-local + deliberately WITHOUT ``voice`` (SMS/MMS has no voice primitive — an MMS
    is a media attachment, the rule-of-three divergence from WhatsApp): ``media`` (an
    MMS attachment) and ``unknown`` (media without a classifiable type).
    """

    media = "media"
    unknown = "unknown"


class InboundText(BaseModel):
    """A text SMS normalised to C1's inbound shape — drives the shared flow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inbound: NormalisedInbound


class InboundNonText(BaseModel):
    """Non-text content (an MMS) — declined gracefully (D-C2-6), never a runtime turn."""

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


def _num_media(form: Mapping[str, str]) -> int:
    """Parse Twilio's ``NumMedia`` count (defensive — defaults to 0)."""
    raw = form.get("NumMedia", "0")
    try:
        return int(raw)
    except ValueError:
        return 0


def _non_text_kind(form: Mapping[str, str]) -> NonTextKind:
    """Classify an MMS by its first media content type (``media`` else ``unknown``)."""
    return NonTextKind.media if form.get("MediaContentType0", "") else NonTextKind.unknown


def classify_inbound(form: Mapping[str, str], *, now: datetime) -> NormalisedInboundMessage:
    """Classify a raw Twilio SMS inbound form into a :data:`NormalisedInboundMessage`.

    Args:
        form: The decoded Twilio inbound form params (``From`` / ``To`` / ``Body`` /
            ``MessageSid`` / ``NumMedia`` / ``MediaContentType0`` / …).
        now: Tz-aware UTC ingestion time — the ``received_at`` (Twilio carries no inbound
            timestamp; injected so this is pure).

    Returns:
        :class:`InboundText` for a text SMS (the C1 ``NormalisedInbound``),
        :class:`InboundNonText` for an MMS, or :class:`InboundIgnore` for an empty /
        malformed inbound.
    """
    message_sid = form.get("MessageSid")
    sender_id = form.get("From")
    if not message_sid or not sender_id:
        return InboundIgnore(reason="malformed")

    if _num_media(form) > 0:
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
            raw["to"] = to
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
