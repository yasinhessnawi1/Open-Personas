"""Email thread → conversation_key derivation (Spec C5, A3 / D-C5-2).

The adapter-side pure half of the platform-native-boundary seam: derive a stable
conversation key from the RFC 5322 threading headers so a thread IS a conversation
and a new email IS a new conversation — **by References/In-Reply-To, never subject**
(``Re:`` prefixes + subject edits are unreliable). The boundary *semantics* (email
opting out of the idle sweep) live in C1 (A1); this only computes the key.
"""

from __future__ import annotations

from persona_connectors.email.thread_key import derive_conversation_key

_ROOT = "<root@mail.example.com>"
_R2 = "<reply-2@mail.example.com>"
_R3 = "<reply-3@mail.example.com>"
_NEW = "<fresh@mail.example.com>"


def test_new_email_keys_on_its_own_message_id() -> None:
    """A genuinely new email (no In-Reply-To / References) roots a new thread — its key
    is its own Message-ID (it becomes the thread root)."""
    key = derive_conversation_key(message_id=_ROOT, in_reply_to=None, references=None)
    assert key == "root@mail.example.com"  # angle brackets stripped, stable


def test_reply_chain_shares_the_root_key() -> None:
    """Every message in a reply chain keys on the thread ROOT (References[0]) — so a
    reply JOINS the originating conversation (criterion 2)."""
    original = derive_conversation_key(message_id=_ROOT, in_reply_to=None, references=None)
    reply = derive_conversation_key(message_id=_R2, in_reply_to=_ROOT, references=_ROOT)
    deep_reply = derive_conversation_key(
        message_id=_R3, in_reply_to=_R2, references=f"{_ROOT} {_R2}"
    )
    assert original == reply == deep_reply == "root@mail.example.com"


def test_in_reply_to_used_when_references_absent() -> None:
    """A reply with In-Reply-To but no References (a 2-message thread) still joins the
    parent — In-Reply-To is the root when References is missing."""
    key = derive_conversation_key(message_id=_R2, in_reply_to=_ROOT, references=None)
    assert key == "root@mail.example.com"


def test_subject_change_does_not_fork_the_thread() -> None:
    """The key derivation NEVER consults the subject — two replies in the same
    reference-chain share a key even if their subjects differ (a renamed ``Re:`` thread
    stays one conversation). Structurally proven: subject is not a parameter."""
    a = derive_conversation_key(message_id=_R2, in_reply_to=_ROOT, references=_ROOT)
    b = derive_conversation_key(message_id=_R3, in_reply_to=_R2, references=f"{_ROOT} {_R2}")
    assert a == b  # same thread despite any subject edit between them


def test_new_thread_is_not_the_same_conversation_as_a_reply() -> None:
    """Non-vacuous: a reply JOINS (shares the root key) and a brand-new email does NOT
    (its own distinct key) — the two are different conversations."""
    reply_key = derive_conversation_key(message_id=_R2, in_reply_to=_ROOT, references=_ROOT)
    new_key = derive_conversation_key(message_id=_NEW, in_reply_to=None, references=None)
    assert reply_key == "root@mail.example.com"
    assert new_key == "fresh@mail.example.com"
    assert reply_key != new_key


def test_references_parsing_is_whitespace_and_bracket_robust() -> None:
    """References is a whitespace-separated list of <message-id>s (folded/multi-space);
    the root is the first, brackets + surrounding whitespace stripped."""
    key = derive_conversation_key(
        message_id=_R3,
        in_reply_to=_R2,
        references=f"  {_ROOT}\r\n  {_R2}   ",
    )
    assert key == "root@mail.example.com"


def test_missing_message_id_and_headers_is_empty_key_guarded() -> None:
    """Degenerate input (the ESP always supplies a Message-ID) — an all-absent case
    yields an empty key rather than crashing; the caller treats an empty key as
    'no thread' (a fresh conversation)."""
    assert derive_conversation_key(message_id="", in_reply_to=None, references=None) == ""
