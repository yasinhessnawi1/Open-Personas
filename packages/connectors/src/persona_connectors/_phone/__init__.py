"""Shared phone-number (SMS + WhatsApp) carrier helpers (Spec C4).

Both phone-number adapters share the same identity shape (an E.164 number from a
signature-verified provider webhook), the same linking carrier (a textable OTP
code), and the same inbound orchestration (classify → decline | redeem | shared
flow). This package holds what is identical between them — the OTP linking carrier
(:mod:`persona_connectors._phone.linking`) and the shared inbound orchestrator
(:mod:`persona_connectors._phone.flow`) — so neither adapter forks it.
"""

from __future__ import annotations

from persona_connectors._phone.flow import PhoneInboundFlow
from persona_connectors._phone.linking import (
    PhoneLinkingService,
    RedeemResult,
    RedeemStatus,
    generate_phone_code,
    normalise_phone_code,
)

__all__ = [
    "PhoneInboundFlow",
    "PhoneLinkingService",
    "RedeemResult",
    "RedeemStatus",
    "generate_phone_code",
    "normalise_phone_code",
]
