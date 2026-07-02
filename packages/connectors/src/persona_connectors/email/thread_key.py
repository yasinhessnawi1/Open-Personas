"""Email thread → ``conversation_key`` derivation (Spec C5, A3 / D-C5-2).

The adapter-side pure half of the platform-native-boundary seam. Email has a real,
native conversation boundary — a mail thread — so the adapter derives the
``conversation_key`` C1 keys the conversation on (D-C1-2) from the RFC 5322 threading
headers, and C1's resolution uses it unchanged (the ``conversation_key`` documented
"derived for flat-stream platforms like SMS/email"). The boundary *semantics* — email
opting out of the idle sweep — live in C1 (A1 / D-C5-2); this only computes the key.

**By References/In-Reply-To, never subject.** ``Re:`` prefixes and subject edits are
unreliable (a renamed thread must not fork; unrelated same-subject mails must not
merge). The authoritative thread chain is the ``References`` header (ancestry,
oldest-first) → its root is the thread identity; ``In-Reply-To`` is the fallback (a
2-message thread); a brand-new email (neither header) roots a new thread on its own
``Message-ID``.

Owned surface — api-free; stdlib only.
"""

from __future__ import annotations

import re

__all__ = ["derive_conversation_key"]

# A message-id token: ``<local@domain>`` — capture the inner, excluding <>/whitespace.
_MESSAGE_ID = re.compile(r"<([^<>\s]+)>")


def _message_ids(header: str | None) -> list[str]:
    """The bracket-stripped message-ids in a header, in order (``References`` / ``In-Reply-To``).

    Robust to folding + multiple spaces (the regex ignores inter-token whitespace). For
    a malformed header with no angle brackets, falls back to whitespace tokens so a
    stray client format still yields *something* stable rather than nothing.
    """
    if not header:
        return []
    found = _MESSAGE_ID.findall(header)
    if found:
        return found
    return [token.strip("<>") for token in header.split() if token.strip("<>")]


def _normalise(message_id: str) -> str:
    """A single message-id, bracket- and whitespace-stripped (``<a@b>`` → ``a@b``)."""
    return message_id.strip().strip("<>").strip()


def derive_conversation_key(
    *, message_id: str, in_reply_to: str | None = None, references: str | None = None
) -> str:
    """Derive the thread's stable ``conversation_key`` from the message headers.

    Precedence (RFC 5322 threading): the ``References`` **root** (``References[0]`` — the
    thread origin every reply copies forward, so all replies share it) → ``In-Reply-To``
    (the parent, when ``References`` is absent — a 2-message thread) → the message's own
    ``Message-ID`` (a brand-new email roots a new thread). Subject is deliberately never
    consulted. Returns the bracket-stripped id; an all-absent input yields ``""`` (the
    caller treats an empty key as a fresh conversation — the ESP always supplies a
    ``Message-ID``, so this is only a degenerate guard).
    """
    references_ids = _message_ids(references)
    if references_ids:
        return references_ids[0]
    parent_ids = _message_ids(in_reply_to)
    if parent_ids:
        return parent_ids[0]
    return _normalise(message_id)
