"""classify_inbound — a Twilio WhatsApp form POST → C1's inbound shape (Spec C4 T4).

Pure, deterministic tests over Twilio's form-encoded inbound params: the text →
NormalisedInbound mapping (esp. the ``whatsapp:`` prefix stripping for the
sender_id/conversation_key, the always-tz-aware received_at), the non-text
classification (NumMedia/MediaContentType0), and the ignore path for empty bodies.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona_connectors.whatsapp.inbound import (
    PLATFORM,
    InboundIgnore,
    InboundNonText,
    InboundText,
    NonTextKind,
    classify_inbound,
)

_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _form(**overrides: str) -> dict[str, str]:
    """A minimal Twilio WhatsApp inbound form payload."""
    form: dict[str, str] = {
        "MessageSid": "SM100",
        "From": "whatsapp:+14155551234",
        "To": "whatsapp:+14155238886",
        "Body": "Astrid, hello",
        "NumMedia": "0",
    }
    form.update(overrides)
    return form


def test_text_message_maps_to_normalised_inbound() -> None:
    """A text message → InboundText (the C1 NormalisedInbound; whatsapp: prefix stripped)."""
    result = classify_inbound(_form(), now=_NOW)

    assert isinstance(result, InboundText)
    inbound = result.inbound
    assert inbound.platform == PLATFORM
    # The whatsapp: prefix is stripped — the identity is the bare E.164 number.
    assert inbound.sender_id == "+14155551234"
    assert inbound.conversation_key == "+14155551234"
    assert inbound.message_id == "SM100"
    assert inbound.text == "Astrid, hello"


def test_received_at_is_tz_aware_utc() -> None:
    """Twilio carries no inbound timestamp → the injected ingestion-time (tz-aware)."""
    result = classify_inbound(_form(), now=_NOW)
    assert isinstance(result, InboundText)
    assert result.inbound.received_at == _NOW
    assert result.inbound.received_at.tzinfo is not None


def test_media_message_maps_to_non_text() -> None:
    """NumMedia > 0 with an image → InboundNonText (media), classified from MediaContentType0."""
    result = classify_inbound(
        _form(Body="", NumMedia="1", MediaContentType0="image/jpeg", MediaUrl0="https://x/y.jpg"),
        now=_NOW,
    )
    assert isinstance(result, InboundNonText)
    assert result.kind is NonTextKind.media
    assert result.conversation_key == "+14155551234"
    assert result.sender_id == "+14155551234"
    assert result.message_id == "SM100"


def test_voice_message_maps_to_non_text_voice() -> None:
    """An audio media type → InboundNonText (voice) — can't listen yet."""
    result = classify_inbound(_form(Body="", NumMedia="1", MediaContentType0="audio/ogg"), now=_NOW)
    assert isinstance(result, InboundNonText)
    assert result.kind is NonTextKind.voice


def test_media_with_caption_is_still_non_text() -> None:
    """A media message with a caption is still non-text (the C2 photo-caption rule)."""
    result = classify_inbound(
        _form(Body="look at this", NumMedia="1", MediaContentType0="image/png"), now=_NOW
    )
    assert isinstance(result, InboundNonText)
    assert result.kind is NonTextKind.media


def test_unknown_media_type_maps_to_unknown() -> None:
    """Media without a classifiable content type → unknown."""
    result = classify_inbound(_form(Body="", NumMedia="1"), now=_NOW)
    assert isinstance(result, InboundNonText)
    assert result.kind is NonTextKind.unknown


def test_empty_body_and_no_media_is_ignored() -> None:
    """No body + no media → ignore (nothing to act on, no reply)."""
    result = classify_inbound(_form(Body="", NumMedia="0"), now=_NOW)
    assert isinstance(result, InboundIgnore)


def test_whitespace_body_is_ignored() -> None:
    """A whitespace-only body with no media → ignore."""
    result = classify_inbound(_form(Body="   ", NumMedia="0"), now=_NOW)
    assert isinstance(result, InboundIgnore)


def test_missing_message_sid_is_ignored() -> None:
    """A malformed payload (no MessageSid / From) → ignore, never a crash."""
    form = _form()
    del form["MessageSid"]
    result = classify_inbound(form, now=_NOW)
    assert isinstance(result, InboundIgnore)
