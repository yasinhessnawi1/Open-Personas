"""R4 T3 — unit tests for conversation-title sanitisation.

Root cause of the reported bug: the titling PROMPT was never stored; a
small/background-tier model echoed its own instruction (or leaked reasoning
about it) into ``message.content`` and that echo was stored verbatim as the
title. ``sanitize_conversation_title`` is the storage-path guard.
"""

from __future__ import annotations

from persona_api.services.chat_service import (
    sanitize_conversation_title,
    sanitize_title_candidate,
)

# The exact class of string the bug produced: the titling instruction echoed
# back (or reasoned about) instead of an actual title.
_INSTRUCTION_ECHO = (
    "We need to output a title of at most 5 words, no quotes, no punctuation, no prose"
)


def test_clean_title_is_preserved() -> None:
    assert (
        sanitize_conversation_title("Norwegian tenancy question", first_message="x")
        == "Norwegian tenancy question"
    )


def test_strips_wrapping_quotes_and_trailing_punctuation() -> None:
    assert sanitize_conversation_title('"Lease question."', first_message="x") == "Lease question"


def test_rejects_instruction_echo_and_falls_back_to_user_words() -> None:
    out = sanitize_conversation_title(
        _INSTRUCTION_ECHO,
        first_message="help me understand my Norwegian lease agreement today",
    )
    assert "at most" not in out.lower()
    assert "no punctuation" not in out.lower()
    # Fallback = the first few words of the user's own message.
    assert out == "help me understand my Norwegian lease"


def test_empty_output_falls_back_to_user_words() -> None:
    assert (
        sanitize_conversation_title("   ", first_message="hello there friend")
        == "hello there friend"
    )


def test_empty_output_and_empty_message_uses_neutral_default() -> None:
    assert sanitize_conversation_title("", first_message="") == "New conversation"


def test_caps_the_word_count() -> None:
    out = sanitize_conversation_title(
        "one two three four five six seven eight nine ten", first_message="x"
    )
    assert out == "one two three four five six seven eight"


def test_takes_only_the_first_non_empty_line() -> None:
    assert (
        sanitize_conversation_title(
            "\n\nBudget planning\nplus some trailing reasoning prose",
            first_message="x",
        )
        == "Budget planning"
    )


# -- R9-020: the STRICT half (no fallback) the title_refresh job consumes ----
# A refresh must be able to DISTINGUISH "bad generation" (None → keep the
# existing title) from "usable title" — the fallback-composing function above
# cannot, so the split itself is the contract under test.


def test_candidate_clean_title_is_returned() -> None:
    assert sanitize_title_candidate("Norwegian tenancy question") == "Norwegian tenancy question"


def test_candidate_rejects_instruction_echo_with_none() -> None:
    assert sanitize_title_candidate(_INSTRUCTION_ECHO) is None


def test_candidate_rejects_empty_and_whitespace_with_none() -> None:
    assert sanitize_title_candidate("") is None
    assert sanitize_title_candidate("   \n  ") is None


def test_candidate_rejects_punctuation_only_with_none() -> None:
    assert sanitize_title_candidate('"..."') is None
    assert sanitize_title_candidate("?!.") is None


def test_candidate_strips_quotes_punctuation_and_caps_words() -> None:
    assert sanitize_title_candidate('"Lease question."') == "Lease question"
    assert (
        sanitize_title_candidate("one two three four five six seven eight nine ten")
        == "one two three four five six seven eight"
    )


def test_fallback_wrapper_matches_the_strict_half() -> None:
    # sanitize_conversation_title == strict-half OR fallback, byte-for-byte.
    assert sanitize_conversation_title("Budget planning", first_message="x") == (
        sanitize_title_candidate("Budget planning")
    )
    assert sanitize_conversation_title(_INSTRUCTION_ECHO, first_message="my words here") == (
        "my words here"
    )
