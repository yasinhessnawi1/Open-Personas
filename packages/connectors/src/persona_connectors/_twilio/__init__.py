"""Shared Twilio I/O for the WhatsApp + SMS adapters (Spec C4).

ONE Twilio account drives BOTH channels (D-C4-1); the channel is chosen by the
``From`` address prefix (``whatsapp:+E164`` vs a bare ``+E164``). The shared pieces
live here so the per-channel adapters (``whatsapp/`` + ``sms/``) reuse them rather
than each re-implementing the Twilio transport + the webhook authenticity gate:

- :class:`~persona_connectors._twilio.client.TwilioClient` — the single Twilio
  Messages-API I/O boundary (form-encoded + HTTP Basic auth; faults → domain errors).
- :func:`~persona_connectors._twilio.webhook.verify_twilio_signature` — the mandatory
  inbound/status webhook authenticity gate (HMAC-SHA1, constant-time, fail-closed).

This package is **api-free** (httpx + persona-core errors only).
"""

from __future__ import annotations

from persona_connectors._twilio.client import (
    TWILIO_MAX_MESSAGE_CHARS,
    TwilioClient,
    TwilioMessageResult,
)
from persona_connectors._twilio.webhook import (
    TWILIO_SIGNATURE_HEADER,
    verify_twilio_signature,
)

__all__ = [
    "TWILIO_MAX_MESSAGE_CHARS",
    "TWILIO_SIGNATURE_HEADER",
    "TwilioClient",
    "TwilioMessageResult",
    "verify_twilio_signature",
]
