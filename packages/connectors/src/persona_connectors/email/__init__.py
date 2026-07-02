"""The email connector adapter (Spec C5) — a persona reachable by email.

The thickest C-track adapter (parsing / threading / auth / verification), but still an
adapter: the core inbound→route→respond→outbound flow, the conversation model, and identity
mapping stay C1's; only email-specific I/O lives here. The package is named ``email`` to match
the platform key (the C-track convention: package == platform); it shadows the stdlib
``email`` module by name only — Python 3 absolute imports resolve ``import email`` to the
stdlib regardless (the A005 per-file-ignore documents this).
"""

from __future__ import annotations

from persona_connectors.email.connector import EMAIL_CAPABILITIES, PLATFORM, EmailConnector
from persona_connectors.email.inbound import ParsedEmail, parse_inbound_email
from persona_connectors.email.parsing import extract_new_content, html_to_text
from persona_connectors.email.render import (
    originated_subject,
    render_from,
    reply_subject,
    thread_headers,
)
from persona_connectors.email.thread_key import derive_conversation_key

__all__ = [
    "EMAIL_CAPABILITIES",
    "PLATFORM",
    "EmailConnector",
    "ParsedEmail",
    "derive_conversation_key",
    "extract_new_content",
    "html_to_text",
    "originated_subject",
    "parse_inbound_email",
    "render_from",
    "reply_subject",
    "thread_headers",
]
