"""Postmark inbound payload → C1 shape (Spec C5, Group D) — the field mapping + routing signals."""

from __future__ import annotations

from datetime import UTC, datetime

from persona_connectors.email.inbound import parse_inbound_email

_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)
_ROOT = "<root@mail.gmail.com>"
_REPLY_ID = "<reply-2@mail.gmail.com>"


def _payload(**overrides: object) -> dict[str, object]:
    """A realistic Postmark inbound-parse payload (a threaded reply, plus-addressed)."""
    base: dict[str, object] = {
        "FromName": "Bob Smith",
        "From": "Bob@Example.com",
        "FromFull": {"Email": "Bob@Example.com", "Name": "Bob Smith"},
        "To": "inbound+astrid@personas.app",
        "ToFull": [{"Email": "inbound+astrid@personas.app", "Name": "", "MailboxHash": "astrid"}],
        "MailboxHash": "astrid",
        "Subject": "Re: Deposit dispute",
        "MessageID": "postmark-uuid-123",  # Postmark's OWN id — not used for threading
        "StrippedTextReply": "What's the deadline for filing?",
        "TextBody": "What's the deadline for filing?\n\nOn Mon ... wrote:\n> earlier",
        "HtmlBody": "<p>What's the deadline for filing?</p>",
        "Attachments": [],
        "Headers": [
            {"Name": "Message-ID", "Value": _REPLY_ID},
            {"Name": "In-Reply-To", "Value": _ROOT},
            {"Name": "References", "Value": f"{_ROOT} {_REPLY_ID}"},
            {
                "Name": "Authentication-Results",
                "Value": "mx.postmark.com; dkim=pass; spf=pass; dmarc=pass (p=REJECT)",
            },
        ],
    }
    base.update(overrides)
    return base


def test_maps_the_core_inbound_fields() -> None:
    parsed = parse_inbound_email(_payload(), now=_NOW)
    assert parsed is not None
    inbound = parsed.inbound
    assert inbound.platform == "email"
    assert inbound.sender_id == "bob@example.com"  # From address, lowercased
    assert inbound.display_name == "Bob Smith"
    assert inbound.received_at == _NOW
    assert inbound.text == "What's the deadline for filing?"  # StrippedTextReply (primary)


def test_conversation_key_is_the_thread_root_not_postmark_id_or_subject() -> None:
    """Threading keys on the RFC References root (A3), NOT Postmark's tracking MessageID."""
    parsed = parse_inbound_email(_payload(), now=_NOW)
    assert parsed is not None
    assert parsed.inbound.conversation_key == "root@mail.gmail.com"  # References[0], brackets off
    assert parsed.inbound.thread_id == "root@mail.gmail.com"
    assert parsed.inbound.reply_to_message_id == _ROOT  # In-Reply-To carried through


def test_new_email_keys_on_its_own_message_id() -> None:
    """A fresh email (no In-Reply-To / References) roots a new thread on its Message-ID."""
    payload = _payload(
        Headers=[{"Name": "Message-ID", "Value": "<fresh@mail.gmail.com>"}],
    )
    parsed = parse_inbound_email(payload, now=_NOW)
    assert parsed is not None
    assert parsed.inbound.conversation_key == "fresh@mail.gmail.com"


def test_extracts_the_routing_signals_for_groups_a2_b2_and_e() -> None:
    parsed = parse_inbound_email(_payload(), now=_NOW)
    assert parsed is not None
    assert parsed.envelope_persona_tag == "astrid"  # MailboxHash → A2 envelope-tag
    assert parsed.authentication_results is not None
    assert "dmarc=pass" in parsed.authentication_results  # → B2 verdict
    assert parsed.subject == "Re: Deposit dispute"  # → the reply Re:
    assert parsed.has_attachments is False


def test_attachments_are_flagged_not_processed() -> None:
    payload = _payload(
        Attachments=[
            {"Name": "contract.pdf", "ContentType": "application/pdf", "ContentLength": 10}
        ]
    )
    parsed = parse_inbound_email(payload, now=_NOW)
    assert parsed is not None
    assert parsed.has_attachments is True


def test_no_mailbox_hash_yields_no_envelope_tag_falls_back_to_naming() -> None:
    payload = _payload(
        MailboxHash="", ToFull=[{"Email": "inbound@personas.app", "MailboxHash": ""}]
    )
    parsed = parse_inbound_email(payload, now=_NOW)
    assert parsed is not None
    assert parsed.envelope_persona_tag is None  # the flow falls back to subject/body naming


def test_malformed_missing_sender_is_none() -> None:
    payload = _payload(From="", FromFull={})
    assert parse_inbound_email(payload, now=_NOW) is None


def test_malformed_missing_message_id_is_none() -> None:
    payload = _payload(MessageID="", Headers=[])
    assert parse_inbound_email(payload, now=_NOW) is None


def test_fallback_parse_when_no_stripped_reply() -> None:
    """No StrippedTextReply → the raw TextBody runs through the mail-parser-reply fallback."""
    payload = _payload(
        StrippedTextReply="",
        TextBody="The new question here.\n\nOn Mon, Jan 1 Astrid wrote:\n> old quoted text",
    )
    parsed = parse_inbound_email(payload, now=_NOW)
    assert parsed is not None
    assert "The new question here." in parsed.inbound.text
    assert "old quoted text" not in parsed.inbound.text
