"""WHATSAPP_CAPABILITIES — the WhatsApp channel's declared capabilities (Spec C4 T1).

The flow never assumes an absent feature; these flags are the WhatsApp facts the
shared flow + the splitter budget against. The load-bearing one is
``can_initiate_freely=False`` — the 24h customer-care window (D-C4-1): a free-form
outbound is gated, the dynamic per-message check + reject mapping lands in the
connector's ``send`` (T9/T13).
"""

from __future__ import annotations

from persona_connectors.whatsapp.connector import WHATSAPP_CAPABILITIES


def test_whatsapp_capabilities_are_exactly_as_specified() -> None:
    """Every WhatsApp capability flag is pinned (the C4 decisions table)."""
    caps = WHATSAPP_CAPABILITIES
    assert caps.supports_rich_formatting is True
    assert caps.supports_author_affordance is False
    assert caps.supports_threads is False
    assert caps.supports_typing_indicator is True
    assert caps.is_realtime_push is True
    # The 24h customer-care window — a free-form outbound is NOT always allowed.
    assert caps.can_initiate_freely is False
    # Twilio enforces a 1600-char hard cap on WhatsApp bodies too (the splitter budget).
    assert caps.max_body_chars == 1600
    assert caps.encoding_sensitive is False
    assert caps.requires_delivery_auth is False
