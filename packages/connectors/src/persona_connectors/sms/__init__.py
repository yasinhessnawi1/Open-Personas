"""The SMS connector adapter (Spec C4) — Twilio-backed, api-free.

A deliberately **thin** adapter implementing C1's ``Connector`` protocol for SMS over
the shared Twilio Messages API (D-C4-1; the channel is the bare ``+E164`` from-address,
no ``whatsapp:`` prefix). It converts Twilio SMS form POSTs to C1's ``NormalisedInbound``,
lets C1's shared flow drive the reply, and renders C1's ``NormalisedOutbound`` back as
Twilio messages — everything else (routing, persona selection, the conversation model,
identity mapping, C0 delivery) is C1's and is *used*, not reimplemented.

SMS is the **floor** (the Email/SMS intersection): no rich formatting / typing / threads,
but free-to-initiate + encoding-sensitive (GSM-7 → UCS-2, the T12 splitter). The whole
adapter is **api-free**; the api-coupling lives in :mod:`persona_connectors.composition`.
Group A (T1–T5) ships the config + capabilities, the inbound normalisation, the non-text
decline, and the flow transport; the send path (the multi-segment split), the linking,
and the webhook app land in later tasks.
"""

from __future__ import annotations

from persona_connectors.sms.connector import SMS_CAPABILITIES, SmsConnector
from persona_connectors.sms.flow import SmsFlowTransport
from persona_connectors.sms.inbound import (
    PLATFORM,
    InboundIgnore,
    InboundNonText,
    InboundText,
    NonTextKind,
    NormalisedInboundMessage,
    classify_inbound,
)
from persona_connectors.sms.non_text import decline_message

__all__ = [
    "PLATFORM",
    "SMS_CAPABILITIES",
    "InboundIgnore",
    "InboundNonText",
    "InboundText",
    "NonTextKind",
    "NormalisedInboundMessage",
    "SmsConnector",
    "SmsFlowTransport",
    "classify_inbound",
    "decline_message",
]
