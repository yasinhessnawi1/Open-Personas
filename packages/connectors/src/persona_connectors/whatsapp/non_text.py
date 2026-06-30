"""Non-text graceful declines (Spec C4 T5, D-C2-6 carried forward) — pure copy.

When :func:`~persona_connectors.whatsapp.inbound.classify_inbound` yields an
:class:`~persona_connectors.whatsapp.inbound.InboundNonText`, the flow replies with a
friendly, text-only-for-now line rather than an error or silence. The line is the
**bot speaking** (system-level), not a persona — so it carries no persona name tag and
is sent as plain text; it never drives a runtime turn.

The taxonomy mirrors Telegram's (WhatsApp has a real voice primitive), so the copy
mirrors ``telegram/non_text.py`` rather than importing it — the kind enum is
WhatsApp-local (:class:`~persona_connectors.whatsapp.inbound.NonTextKind`), so binding
the copy to the local enum keeps each adapter self-contained.

Pure + api-free: a total function over the enum (``assert_never`` guarantees every kind
is handled), no I/O, no ``persona_api``.
"""

from __future__ import annotations

from typing import assert_never

from persona_connectors.whatsapp.inbound import NonTextKind

__all__ = ["decline_message"]


def decline_message(kind: NonTextKind) -> str:
    """Return the friendly text-only decline for a non-text WhatsApp message (D-C2-6).

    Args:
        kind: The classified non-text content kind.

    Returns:
        A single product-voice line making clear the persona works over text for now.
    """
    match kind:
        case NonTextKind.voice:
            return "I can't listen to voice messages yet — send me a text message and I'll reply."
        case NonTextKind.media:
            return "I work over text for now — type me a message and I'm all yours."
        case NonTextKind.unknown:
            return "I work over text — send me a message and I'll reply."
        case _:  # pragma: no cover - exhaustiveness guard (mypy assert_never)
            assert_never(kind)
