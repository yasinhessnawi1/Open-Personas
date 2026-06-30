"""classify_inbound — a Twilio SMS form POST → C1's inbound shape (Spec C4 T4).

Pure, deterministic tests over Twilio's form-encoded SMS inbound: the text →
NormalisedInbound mapping (the bare E.164 number is the identity — no whatsapp:
prefix), the MMS non-text classification, and the ignore path for empty bodies.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona_connectors.sms.inbound import (
    PLATFORM,
    InboundIgnore,
    InboundNonText,
    InboundText,
    NonTextKind,
    classify_inbound,
)

_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _form(**overrides: str) -> dict[str, str]:
    """A minimal Twilio SMS inbound form payload."""
    form: dict[str, str] = {
        "MessageSid": "SM200",
        "From": "+14155551234",
        "To": "+14155238886",
        "Body": "Astrid, hello",
        "NumMedia": "0",
    }
    form.update(overrides)
    return form


def test_text_message_maps_to_normalised_inbound() -> None:
    """A text SMS → InboundText carrying the C1 NormalisedInbound (E.164 = identity)."""
    result = classify_inbound(_form(), now=_NOW)

    assert isinstance(result, InboundText)
    inbound = result.inbound
    assert inbound.platform == PLATFORM
    assert inbound.sender_id == "+14155551234"
    assert inbound.conversation_key == "+14155551234"
    assert inbound.message_id == "SM200"
    assert inbound.text == "Astrid, hello"


def test_received_at_is_tz_aware_utc() -> None:
    """No inbound timestamp from Twilio → the injected ingestion-time (tz-aware)."""
    result = classify_inbound(_form(), now=_NOW)
    assert isinstance(result, InboundText)
    assert result.inbound.received_at == _NOW
    assert result.inbound.received_at.tzinfo is not None


def test_mms_maps_to_non_text() -> None:
    """An MMS (NumMedia > 0) → InboundNonText (media)."""
    result = classify_inbound(
        _form(Body="", NumMedia="1", MediaContentType0="image/jpeg"), now=_NOW
    )
    assert isinstance(result, InboundNonText)
    assert result.kind is NonTextKind.media
    assert result.conversation_key == "+14155551234"
    assert result.message_id == "SM200"


def test_empty_body_and_no_media_is_ignored() -> None:
    """No body + no media → ignore."""
    result = classify_inbound(_form(Body="", NumMedia="0"), now=_NOW)
    assert isinstance(result, InboundIgnore)


def test_missing_message_sid_is_ignored() -> None:
    """A malformed payload (no MessageSid / From) → ignore, never a crash."""
    form = _form()
    del form["From"]
    result = classify_inbound(form, now=_NOW)
    assert isinstance(result, InboundIgnore)
