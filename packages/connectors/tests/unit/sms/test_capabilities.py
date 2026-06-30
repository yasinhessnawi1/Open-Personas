"""SMS_CAPABILITIES — the SMS channel's declared capabilities (Spec C4 T1).

SMS is the floor (the Email/SMS intersection C1 designs to): no rich formatting,
no typing, no threads — but it CAN initiate freely (no WhatsApp-style window) and
is encoding-sensitive (GSM-7 → UCS-2 changes the length budget, T12). The hard
per-message cap is Twilio's 1600 characters.
"""

from __future__ import annotations

from persona_connectors.sms.connector import SMS_CAPABILITIES


def test_sms_capabilities_are_exactly_as_specified() -> None:
    """Every SMS capability flag is pinned (the C4 decisions table)."""
    caps = SMS_CAPABILITIES
    assert caps.supports_rich_formatting is False
    assert caps.supports_author_affordance is False
    assert caps.supports_threads is False
    assert caps.supports_typing_indicator is False
    assert caps.is_realtime_push is True
    # SMS can send a free-form outbound at any time (no 24h window).
    assert caps.can_initiate_freely is True
    # Twilio's hard SMS body cap (concatenated segments).
    assert caps.max_body_chars == 1600
    # GSM-7 → UCS-2: a non-Latin/emoji char changes the segment budget (T12).
    assert caps.encoding_sensitive is True
    assert caps.requires_delivery_auth is False
