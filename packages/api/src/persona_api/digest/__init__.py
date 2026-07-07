"""persona_api.digest — the morning-review digest (Spec A6, B5).

The one shared digest builder (A6 owns it; C0 consumes it — the C6/K5 one-mechanism discipline):
a render-agnostic ``MorningDigest`` composed from the durable A0–A5 state, plus the concrete
:class:`DeferredDigestStore` that captures over-cap chatter as a secondary input.
"""

from __future__ import annotations

from persona_api.digest.builder import (
    DigestItem,
    DigestSection,
    MorningDigest,
    UpcomingItem,
    build_morning_digest,
    render_digest_message,
)
from persona_api.digest.store import DeferredDigestItem, DeferredDigestStore

__all__ = [
    "DeferredDigestItem",
    "DeferredDigestStore",
    "DigestItem",
    "DigestSection",
    "MorningDigest",
    "UpcomingItem",
    "build_morning_digest",
    "render_digest_message",
]
