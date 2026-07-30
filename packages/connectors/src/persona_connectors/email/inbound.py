"""Inbound normalisation — a Postmark inbound-parse payload → C1's shape (Spec C5, Group D).

Maps Postmark's inbound JSON to a :class:`~persona_connectors.domain.normalise.NormalisedInbound`
plus the email-specific routing signals the webhook (Group E) needs downstream:

- **conversation_key** — the thread boundary, from the RFC 5322 ``Message-ID`` /
  ``In-Reply-To`` / ``References`` headers via :func:`derive_conversation_key` (A3) — NOT
  Postmark's own tracking ``MessageID`` and NOT the subject.
- **envelope_persona_tag** — Postmark's ``MailboxHash`` (``inbound+astrid@`` → ``astrid``),
  the deterministic persona selector fed to the flow's ``envelope_persona_tag`` (A2 / D-C5-3).
- **authentication_results** / **spam_tests** / **received_spf** — the raw headers the B2
  sender-authenticity gate (:mod:`persona_connectors._postmark.authentication`) decides over
  (trusted only post-B1). Postmark's real inbound payload never carries
  ``Authentication-Results`` — ``spam_tests`` (``X-Spam-Tests``, SpamAssassin's
  ``DKIM_VALID_AU`` token) is the aligned signal B2 actually keys on; ``received_spf`` is
  carried through for diagnostics only, never for authorisation (SPF alone doesn't protect
  ``From:``).
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

__all__ = ["ParsedEmail", "missing_inbound_field", "parse_inbound_email"]


class ParsedEmail(BaseModel):
    """A Postmark inbound payload normalised to C1's inbound + the email routing signals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    inbound: NormalisedInbound
    envelope_persona_tag: str | None
    subject: str
    references: str | None
    authentication_results: str | None
    spam_tests: str | None
    received_spf: str | None
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


def _extract_sender(payload: Mapping[str, object]) -> str:
    """The lower-cased ``From`` address (``FromFull.Email`` first), or ``""`` if absent."""
    from_full = payload.get("FromFull")
    sender = _str(from_full.get("Email")) if isinstance(from_full, dict) else ""
    if not sender:
        sender = _str(payload.get("From"))
    return sender.lower()


def _extract_message_id(headers: Mapping[str, str], payload: Mapping[str, object]) -> str:
    """The RFC 5322 ``Message-ID``, else Postmark's own tracking id, else ``""``."""
    return headers.get("message-id", "").strip() or _str(payload.get("MessageID"))


def missing_inbound_field(payload: Mapping[str, object]) -> str:
    """Name the field whose absence makes :func:`parse_inbound_email` return ``None``.

    R9-077 (observability): an unparsable payload is a **200 no-op** — Postmark must not
    retry a bad body — so without this the webhook drops the message with no record of
    why. The caller logs the returned field name; it shares the extractors with
    :func:`parse_inbound_email`, so the two can never drift.

    Returns:
        ``"sender"`` / ``"message_id"`` / ``"conversation_key"`` — the first field the
        parser found absent — or ``""`` when the payload parses.
    """
    if not _extract_sender(payload):
        return "sender"
    headers = _headers_map(payload)
    message_id = _extract_message_id(headers, payload)
    if not message_id:
        return "message_id"
    if not derive_conversation_key(
        message_id=message_id,
        in_reply_to=headers.get("in-reply-to"),
        references=headers.get("references"),
    ):
        return "conversation_key"
    return ""


def parse_inbound_email(payload: Mapping[str, object], *, now: datetime) -> ParsedEmail | None:
    """Normalise a Postmark inbound-parse payload, or ``None`` if malformed (no sender / id).

    ``now`` is the tz-aware UTC ingestion time (the ``received_at`` — the everywhere-aware
    rule). Returns ``None`` (silently skipped upstream) when the ``From`` address or a usable
    message id is absent; :func:`missing_inbound_field` names which one, for the caller's log.
    """
    from_full = payload.get("FromFull")
    sender = _extract_sender(payload)
    if not sender:
        return None

    headers = _headers_map(payload)
    message_id = _extract_message_id(headers, payload)
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
        spam_tests=headers.get("x-spam-tests"),
        received_spf=headers.get("received-spf"),
        has_attachments=bool(isinstance(attachments, list) and attachments),
    )


def _opt(value: object) -> str | None:
    """A payload string field as ``str | None`` (for the parsing helper's optionals)."""
    return value if isinstance(value, str) else None
