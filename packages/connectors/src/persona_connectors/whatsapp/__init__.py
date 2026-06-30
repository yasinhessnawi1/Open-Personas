"""The WhatsApp connector adapter (Spec C4) — Twilio-backed, api-free.

A deliberately **thin** adapter implementing C1's ``Connector`` protocol for WhatsApp
over the shared Twilio Messages API (D-C4-1; the channel is chosen by the
``whatsapp:+E164`` from-address prefix). It converts Twilio WhatsApp form POSTs to
C1's ``NormalisedInbound``, lets C1's shared flow drive the reply, and renders C1's
``NormalisedOutbound`` back as Twilio messages — everything else (routing, persona
selection, the conversation model, identity mapping, C0 delivery) is C1's and is
*used*, not reimplemented.

The whole adapter is **api-free** (it depends only on C1's owned-surface ports +
persona-core contracts + the shared Twilio client); the api-coupling lives in
:mod:`persona_connectors.composition`. Group A (T1–T5) ships the config + capabilities,
the inbound normalisation, the non-text decline, and the flow transport; the send path
(the 24h-window gate), the linking, and the webhook app land in later tasks.
"""

from __future__ import annotations

from persona_connectors.whatsapp.connector import WHATSAPP_CAPABILITIES, WhatsAppConnector
from persona_connectors.whatsapp.flow import WhatsAppFlowTransport
from persona_connectors.whatsapp.inbound import (
    PLATFORM,
    WHATSAPP_PREFIX,
    InboundIgnore,
    InboundNonText,
    InboundText,
    NonTextKind,
    NormalisedInboundMessage,
    classify_inbound,
)
from persona_connectors.whatsapp.non_text import decline_message

__all__ = [
    "PLATFORM",
    "WHATSAPP_CAPABILITIES",
    "WHATSAPP_PREFIX",
    "InboundIgnore",
    "InboundNonText",
    "InboundText",
    "NonTextKind",
    "NormalisedInboundMessage",
    "WhatsAppConnector",
    "WhatsAppFlowTransport",
    "classify_inbound",
    "decline_message",
]
