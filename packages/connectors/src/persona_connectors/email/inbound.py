"""Inbound normalisation — a Postmark inbound-parse payload → C1's shape (Spec C5, Group D).

Maps Postmark's inbound JSON to a :class:`~persona_connectors.domain.normalise.NormalisedInbound`
plus the email-specific routing signals the webhook (Group E) needs downstream:

- **conversation_key** — the thread boundary, from the RFC 5322 ``Message-ID`` /
  ``In-Reply-To`` / ``References`` headers via :func:`derive_conversation_key` (A3) — NOT
  Postmark's own tracking ``MessageID`` and NOT the subject.
- **envelope_persona_tag** — Postmark's ``MailboxHash`` (``inbound+astrid@`` → ``astrid``),
  the deterministic persona selector fed to the flow's ``envelope_persona_tag`` (A2 / D-C5-3).
- **authentication_results** — the ESP-stamped ``Authentication-Results`` header, for the B2
  sender-authenticity gate (trusted only post-B1).
- **subject** / **references** — for the reply's ``Re:`` + threading (Group E).
- **has_attachments** — attachments are acknowledged, not processed (v1, criterion 9).

Pure + api-free: deterministic over its input (``now`` injected), no I/O, no ``persona_api``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from persona_connectors.domain.normalise import NormalisedInbound
from persona_connectors.email.connector import PLATFORM
from persona_connectors.email.parsing import extract_new_content
from persona_connectors.email.thread_key import derive_conversation_key

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

__all__ = ["ParsedEmail", "parse_inbound_email"]


class ParsedEmail(BaseModel):
    """A Postmark inbound payload normalised to C1's inbound + the email routing signals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inbound: NormalisedInbound
    envelope_persona_tag: str | None
    subject: str
    references: str | None
    authentication_results: str | None
    has_attachments: bool


def _str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _headers_map(payload: Mapping[str, object]) -> dict[str, str]:
    """Case-insensitive ``header-name → value`` map from Postmark's ``Headers`` array."""
    raw = payload.get("Headers")
    headers: dict[str, str] = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                name = _str(entry.get("Name")).lower()
                if name:
                    headers[name] = str(entry.get("Value", ""))
    return headers


def parse_inbound_email(payload: Mapping[str, object], *, now: datetime) -> ParsedEmail | None:
    """Normalise a Postmark inbound-parse payload, or ``None`` if malformed (no sender / id).

    ``now`` is the tz-aware UTC ingestion time (the ``received_at`` — the everywhere-aware
    rule). Returns ``None`` (silently skipped upstream) when the ``From`` address or a usable
    message id is absent.
    """
    from_full = payload.get("FromFull")
    sender = _str(from_full.get("Email")) if isinstance(from_full, dict) else ""
    if not sender:
        sender = _str(payload.get("From"))
    if not sender:
        return None
    sender = sender.lower()

    headers = _headers_map(payload)
    message_id = headers.get("message-id", "").strip() or _str(payload.get("MessageID"))
    if not message_id:
        return None
    in_reply_to = headers.get("in-reply-to")
    references = headers.get("references")
    conversation_key = derive_conversation_key(
        message_id=message_id, in_reply_to=in_reply_to, references=references
    )
    if not conversation_key:
        return None

    text = extract_new_content(
        stripped_text_reply=_opt(payload.get("StrippedTextReply")),
        text_body=_opt(payload.get("TextBody")),
        html_body=_opt(payload.get("HtmlBody")),
    )
    subject = _str(payload.get("Subject"))
    mailbox_hash = _str(payload.get("MailboxHash")) or None
    display_name = _str(from_full.get("Name")) if isinstance(from_full, dict) else ""
    attachments = payload.get("Attachments")
    inbound = NormalisedInbound(
        platform=PLATFORM,
        sender_id=sender,
        conversation_key=conversation_key,
        message_id=message_id,
        text=text,
        received_at=now,
        reply_to_message_id=in_reply_to.strip() if in_reply_to else None,
        thread_id=conversation_key,
        display_name=display_name or None,
        raw={"subject": subject},
    )
    return ParsedEmail(
        inbound=inbound,
        envelope_persona_tag=mailbox_hash,
        subject=subject,
        references=references,
        authentication_results=headers.get("authentication-results"),
        has_attachments=bool(isinstance(attachments, list) and attachments),
    )


def _opt(value: object) -> str | None:
    """A payload string field as ``str | None`` (for the parsing helper's optionals)."""
    return value if isinstance(value, str) else None
