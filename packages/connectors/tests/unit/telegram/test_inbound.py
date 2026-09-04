"""classify_update — Telegram Update → C1's inbound shape (Spec C2 T2).

Pure, deterministic tests over raw Telegram ``Update`` payloads: the text→
NormalisedInbound mapping (esp. the Unix-date→tz-aware-UTC and from.id/chat.id
mapping research flagged), the non-text classification (D-C2-6), and the
ignore path for non-message / service / malformed updates.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona_connectors.telegram.inbound import (
    PLATFORM,
    InboundIgnore,
    InboundNonText,
    InboundText,
    NonTextKind,
    classify_update,
)

# A fixed ingestion-time fallback (tz-aware) for date-less messages.
_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)

# 2026-06-25T10:00:00Z as a Unix timestamp (the value Telegram sends in `date`).
_TG_DATE = int(datetime(2026, 6, 25, 10, 0, 0, tzinfo=UTC).timestamp())


def _text_update(text: str, **overrides: object) -> dict[str, object]:
    """A minimal Telegram text-message Update."""
    message: dict[str, object] = {
        "message_id": 100,
        "from": {"id": 777, "first_name": "Ada", "username": "ada_l"},
        "chat": {"id": 555, "type": "private"},
        "date": _TG_DATE,
        "text": text,
    }
    message.update(overrides)
    return {"update_id": 1, "message": message}


def test_text_message_maps_to_normalised_inbound() -> None:
    """A text message → InboundText carrying the C1 NormalisedInbound (6-field core)."""
    result = classify_update(_text_update("Astrid, hello"), now=_NOW)

    assert isinstance(result, InboundText)
    inbound = result.inbound
    assert inbound.platform == PLATFORM
    assert inbound.sender_id == "777"  # from.id — the identity key (linking + resolution)
    assert inbound.conversation_key == "555"  # chat.id — the channel key
    assert inbound.message_id == "100"
    assert inbound.text == "Astrid, hello"
    assert inbound.display_name == "Ada"


def test_date_is_converted_to_tz_aware_utc() -> None:
    """Telegram's Unix `date` → a tz-aware UTC datetime (the everywhere-aware rule)."""
    result = classify_update(_text_update("hi"), now=_NOW)
    assert isinstance(result, InboundText)
    received = result.inbound.received_at
    assert received.tzinfo is not None
    assert received == datetime(2026, 6, 25, 10, 0, 0, tzinfo=UTC)


def test_missing_date_falls_back_to_ingestion_time() -> None:
    """No usable `date` → the injected ingestion-time fallback (still tz-aware)."""
    update = _text_update("hi")
    del update["message"]["date"]  # type: ignore[index]
    result = classify_update(update, now=_NOW)
    assert isinstance(result, InboundText)
    assert result.inbound.received_at == _NOW


def test_display_name_prefers_first_last_then_username() -> None:
    """Display name = first(+last); falls back to @username; else None."""
    full = classify_update(
        _text_update("hi", **{"from": {"id": 1, "first_name": "Ada", "last_name": "Lovelace"}}),
        now=_NOW,
    )
    assert isinstance(full, InboundText)
    assert full.inbound.display_name == "Ada Lovelace"

    uname = classify_update(
        _text_update("hi", **{"from": {"id": 1, "username": "ada_l"}}), now=_NOW
    )
    assert isinstance(uname, InboundText)
    assert uname.inbound.display_name == "ada_l"

    anon = classify_update(_text_update("hi", **{"from": {"id": 1}}), now=_NOW)
    assert isinstance(anon, InboundText)
    assert anon.inbound.display_name is None


def test_reply_to_message_id_is_extracted_when_quoting() -> None:
    """A reply quotes another message → reply_to_message_id is captured."""
    result = classify_update(_text_update("yes", reply_to_message=({"message_id": 42})), now=_NOW)
    assert isinstance(result, InboundText)
    assert result.inbound.reply_to_message_id == "42"


def test_command_text_is_still_text() -> None:
    """A `/start <token>` or `/new` command is text — the router inspects it later (thin T2)."""
    result = classify_update(_text_update("/start abc123"), now=_NOW)
    assert isinstance(result, InboundText)
    assert result.inbound.text == "/start abc123"


@pytest.mark.parametrize(
    ("content_key", "expected"),
    [
        ("voice", NonTextKind.voice),
        ("video_note", NonTextKind.voice),
        ("photo", NonTextKind.media),
        ("video", NonTextKind.media),
        ("document", NonTextKind.media),
        ("sticker", NonTextKind.media),
        ("audio", NonTextKind.media),
        ("animation", NonTextKind.media),
    ],
)
def test_non_text_content_is_classified(content_key: str, expected: NonTextKind) -> None:
    """Voice/media content → InboundNonText with the right kind (D-C2-6, criterion 8)."""
    update = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "from": {"id": 9},
            "chat": {"id": 3, "type": "private"},
            "date": _TG_DATE,
            content_key: {"file_id": "x"},
        },
    }
    result = classify_update(update, now=_NOW)
    assert isinstance(result, InboundNonText)
    assert result.kind == expected
    assert result.conversation_key == "3"
    assert result.message_id == "5"
    assert result.sender_id == "9"


def test_photo_with_caption_is_still_non_text() -> None:
    """A captioned photo is non-text in v1 — the image is the content (D-C2-6)."""
    update = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "from": {"id": 9},
            "chat": {"id": 3, "type": "private"},
            "date": _TG_DATE,
            "photo": [{"file_id": "x"}],
            "caption": "look at this",
        },
    }
    result = classify_update(update, now=_NOW)
    assert isinstance(result, InboundNonText)
    assert result.kind == NonTextKind.media


def test_unknown_content_is_ignored_not_answered() -> None:
    """Content we cannot classify is ignored like a service message, never declined (R9-082).

    Answering it is what littered the owner's chat with "I work over text" before every
    single message: an unrecognised-content message arrived a quarter second before the
    real text, and the flow replied to it. A decline is only right when the user clearly
    sent something we cannot use; for content we cannot even name, say nothing.
    """
    update = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "from": {"id": 9},
            "chat": {"id": 3, "type": "private"},
            "date": _TG_DATE,
            "poll": {"id": "p"},
        },
    }
    result = classify_update(update, now=_NOW)
    assert isinstance(result, InboundIgnore)
    assert result.reason == "unrecognised-content"


def test_service_message_is_ignored() -> None:
    """A service/system message (group join) is ignored — no reply, no decline."""
    update = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "from": {"id": 9},
            "chat": {"id": 3, "type": "group"},
            "date": _TG_DATE,
            "new_chat_members": [{"id": 12}],
        },
    }
    result = classify_update(update, now=_NOW)
    assert isinstance(result, InboundIgnore)
    assert result.reason == "service-message"


def test_non_message_update_is_ignored() -> None:
    """A non-message update (edited message / reaction / channel post) is ignored."""
    result = classify_update({"update_id": 1, "edited_message": {"message_id": 5}}, now=_NOW)
    assert isinstance(result, InboundIgnore)
    assert result.reason == "non-message-update"


def test_malformed_message_is_ignored() -> None:
    """A message missing the identity/chat/message keys is ignored, not a crash."""
    result = classify_update({"update_id": 1, "message": {"text": "hi"}}, now=_NOW)
    assert isinstance(result, InboundIgnore)
    assert result.reason == "malformed-message"
