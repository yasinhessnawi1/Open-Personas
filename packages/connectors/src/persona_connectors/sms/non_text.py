"""Non-text graceful declines (Spec C4 T5, D-C2-6 carried forward) — pure copy.

When :func:`~persona_connectors.sms.inbound.classify_inbound` yields an
:class:`~persona_connectors.sms.inbound.InboundNonText` (an MMS), the flow replies with
a friendly, text-only-for-now line rather than an error or silence. The line is the
**bot speaking** (system-level), not a persona — plain text, no name tag; it never
drives a runtime turn. The SMS kind enum is SMS-local (just ``media`` + ``unknown`` —
no voice), so the copy is bound to it here rather than imported from another adapter.

Pure + api-free: a total function over the enum (``assert_never`` guarantees every kind
is handled), no I/O, no ``persona_api``.
"""

from __future__ import annotations

from typing import assert_never

from persona_connectors.sms.inbound import NonTextKind

__all__ = ["decline_message"]


def decline_message(kind: NonTextKind) -> str:
    """Return the friendly text-only decline for a non-text SMS/MMS message (D-C2-6).

    Args:
        kind: The classified non-text content kind.

    Returns:
        A single product-voice line making clear the persona works over text for now.
    """
    match kind:
        case NonTextKind.media:
            return "I work over text for now — type me a message and I'm all yours."
        case NonTextKind.unknown:
            return "I work over text — send me a message and I'll reply."
        case _:  # pragma: no cover - exhaustiveness guard (mypy assert_never)
            assert_never(kind)
